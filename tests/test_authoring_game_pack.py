import hashlib
import io
import json
import math
import shutil
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.game_pack import load_game_pack
from vntts_artifacts.generated_audio import (
    GeneratedAudioIndex,
    write_generated_audio_manifest,
)
from vntts_artifacts.story_index import write_story_index_document
from vntts_artifacts.voice_generation_queue import write_voice_generation_queue
from vntts_artifacts.voice_manifest import write_voice_manifest

import vntts.authoring.bulk_generation as bulk_generation_module
import vntts.authoring.game_pack as game_pack_module
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    authorize_live_fallback,
    review_generation_item,
    run_bulk_generation,
)
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.game_pack import FinalGamePackError, publish_final_game_pack
from vntts.authoring.missing_voice_policy import NARRATOR_ROLES, MissingVoicePolicy
from vntts.game_pack import import_game_pack
from vntts.synthesis import (
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisResult,
    SynthesisTiming,
)


def audio_samples(sample_rate=16_000):
    indexes = np.arange(sample_rate // 4, dtype=np.float32)
    return (0.25 * np.sin(2 * math.pi * 220 * indexes / sample_rate)).astype(np.float32)


class SyntheticRenderer:
    name = "synthetic"
    model_name = "synthetic-v1"

    def render(self, request):
        pcm = audio_samples()

        def produce():
            yield SynthesisChunk(pcm, 16_000, 0, 1.0)
            return SynthesisResult(
                pcm=pcm,
                sample_rate=16_000,
                completion=SynthesisCompletion.COMPLETE,
                limits=SynthesisLimits(256, 180.0),
                timing=SynthesisTiming(1.0, 2.0),
                diagnostics=SynthesisDiagnostics(
                    backend=self.name,
                    cache_source="fresh-generation",
                    generation_profile=request.generation_profile,
                    seed=request.seed,
                    chunk_count=1,
                    sample_count=len(pcm),
                ),
            )

        return SynthesisChunkStream(produce())


def queue_item(name):
    text = f"Exact text for {name}."
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "record_type": "generation_item",
        "queue_id": f"line:{name}:{text_hash[:16]}",
        "line_id": f"line:{name}",
        "text_sha256": text_hash,
        "text": text,
        "speaker": "Hero",
        "voice_character": "Hero",
        "action": "generate",
    }


def prepare_authoring_fixture(
    root,
    names=("one", "two"),
    *,
    bind_sources=True,
    legacy_narrator=False,
    narrator_selection_character=None,
    named_narrator_fallback=None,
):
    root.mkdir(parents=True, exist_ok=True)
    items = [queue_item(name) for name in names]
    if legacy_narrator:
        for item in items:
            item["speaker"] = "???"
    if named_narrator_fallback is not None:
        for item in items:
            item["speaker"] = named_narrator_fallback
            item["voice_character"] = named_narrator_fallback
    story = root / "inputs" / "story-index.jsonl"
    write_story_index_document(
        story,
        {
            "game": "Synthetic Game",
            "language": "en",
            "generated_at": "2026-08-16T12:00:00+00:00",
        },
        [
            {
                "record_type": "line",
                "line_id": item["line_id"],
                "chapter": "chapter-one",
                "sequence": index,
                "speaker": item["speaker"],
                "voice_character": item["voice_character"],
                "text": item["text"],
                "text_sha256": item["text_sha256"],
                "kind": "dialogue",
                "source_audio_status": "absent",
            }
            for index, item in enumerate(items, start=1)
        ],
    )
    reference = root / "inputs" / "references" / "hero.wav"
    write_pcm16_wav(reference, audio_samples(), 16_000)
    voices = root / "inputs" / "voice-manifest.json"
    write_voice_manifest(
        voices,
        {
            "version": 2,
            "voices": [
                {
                    "character": "Hero",
                    "speaker": "synthetic-hero",
                    "references": ["references/hero.wav"],
                }
            ],
        },
    )
    queue = root / "inputs" / "queue.jsonl"
    metadata = {"game": "Synthetic Game", "language": "en"}
    if bind_sources:
        metadata.update(
            {
                "source_story_index": str(story),
                "source_story_index_sha256": sha256_file(story),
                "source_voice_manifest": str(voices),
                "source_voice_manifest_sha256": sha256_file(voices),
            }
        )
    write_voice_generation_queue(queue, metadata, items)
    output = root / "authoring"
    control_files = {
        "voice_manifest": voices,
        "voice_reference:0001": reference,
    }
    if narrator_selection_character is not None:
        control_files[f"narrator_selection:{narrator_selection_character}"] = reference
    result = run_bulk_generation(
        queue,
        output,
        SyntheticRenderer(),
        provider="synthetic",
        model="synthetic-v1",
        generation_profile="stable",
        control_files=control_files,
        synthesis_character_overrides=(
            None
            if named_narrator_fallback is None
            else {named_narrator_fallback: "Narrator"}
        ),
        missing_voice_policy=(
            None
            if named_narrator_fallback is None
            else MissingVoicePolicy(
                NARRATOR_ROLES, (named_narrator_fallback,)
            ).to_document()
        ),
        narrator_character=(
            narrator_selection_character
            if named_narrator_fallback is not None
            else None
        ),
    )
    return {
        "items": items,
        "story": story,
        "reference": reference,
        "voices": voices,
        "queue": queue,
        "output": output,
        "state": result.state,
        "manifest": result.manifest,
    }


def publish(fixture, destination, **overrides):
    options = {
        "state_path": fixture["state"],
        "queue_path": fixture["queue"],
        "story_index_path": fixture["story"],
        "voice_manifest_path": fixture["voices"],
        "game_id": "synthetic-game",
        "game_version": "1.0",
        "producers": [{"name": "synthetic-producer", "version": "1.0"}],
        "created_at": "2026-08-16T12:05:00+00:00",
    }
    options.update(overrides)
    return publish_final_game_pack(destination, **options)


class AuthoringGamePackTest(unittest.TestCase):
    def test_explicit_live_fallback_is_terminal_and_published_losslessly(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root / "source")
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "approved"
            )
            review_generation_item(
                fixture["state"], fixture["items"][1]["queue_id"], "rejected"
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = authoring_main(
                    [
                        "live-fallback",
                        "--state",
                        str(fixture["state"]),
                        "--queue",
                        str(fixture["queue"]),
                        "--reason",
                        "generated_audio_rejected",
                        "--model",
                        "pocket-tts",
                        fixture["items"][1]["queue_id"],
                    ]
                )
            decision = json.loads(stdout.getvalue())
            result = publish(fixture, root / "pack")
            pack = load_game_pack(result.manifest)
            generated = GeneratedAudioIndex.load(pack.generated_audio.path)
            ledger = generated.metadata["vntts.authoring.live_fallback"]

        self.assertEqual(exit_code, 0)
        self.assertEqual(result.approved_count, 1)
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual(result.live_fallback_count, 1)
        self.assertEqual(ledger["mode"], "explicit")
        self.assertEqual(len(ledger["entries"]), 1)
        self.assertEqual(ledger["entries"][0]["reason"], "generated_audio_rejected")
        self.assertEqual(ledger["entries"][0]["provider"], "pocket-tts")
        self.assertEqual(
            ledger["entries"][0]["decision_sha256"],
            game_pack_module._canonical_sha256(decision),
        )

    def test_live_fallback_refuses_pending_or_unbound_backend(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root / "source", names=("one",))
            before = fixture["state"].read_bytes()
            with self.assertRaisesRegex(BulkGenerationError, "reviewed rejected WAV"):
                authorize_live_fallback(
                    fixture["state"],
                    fixture["queue"],
                    fixture["items"][0]["queue_id"],
                    reason="generated_audio_rejected",
                    model="pocket-tts",
                )
            with self.assertRaisesRegex(BulkGenerationError, "Pocket TTS"):
                authorize_live_fallback(
                    fixture["state"],
                    fixture["queue"],
                    fixture["items"][0]["queue_id"],
                    reason="reference_unavailable_after_audit",
                    provider="moss-tts",
                    model="moss-v1",
                )
            after = fixture["state"].read_bytes()

        self.assertEqual(after, before)

    def test_live_fallback_rejects_state_change_during_staging(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root / "source", names=("one",))
            queue_id = fixture["items"][0]["queue_id"]
            review_generation_item(fixture["state"], queue_id, "rejected")
            real_write = bulk_generation_module._write_generated_manifest_from_state

            def mutate_state(*args, **kwargs):
                result = real_write(*args, **kwargs)
                state = json.loads(fixture["state"].read_text(encoding="utf-8"))
                state["external_change"] = True
                fixture["state"].write_text(
                    json.dumps(state, sort_keys=True), encoding="utf-8"
                )
                return result

            with patch.object(
                bulk_generation_module,
                "_write_generated_manifest_from_state",
                side_effect=mutate_state,
            ):
                with self.assertRaisesRegex(BulkGenerationError, "state changed"):
                    authorize_live_fallback(
                        fixture["state"],
                        fixture["queue"],
                        queue_id,
                        reason="generated_audio_rejected",
                        model="pocket-tts",
                    )

            state = json.loads(fixture["state"].read_text(encoding="utf-8"))

        self.assertNotIn("live_fallback", state["items"][queue_id])

    def test_audited_missing_reference_can_be_terminal_without_a_wav(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root / "source")
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "approved"
            )
            state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            state["items"].pop(fixture["items"][1]["queue_id"])
            fixture["state"].write_text(
                json.dumps(state, sort_keys=True), encoding="utf-8"
            )
            authorize_live_fallback(
                fixture["state"],
                fixture["queue"],
                fixture["items"][1]["queue_id"],
                reason="reference_unavailable_after_audit",
                model="pocket-tts",
            )
            result = publish(fixture, root / "pack")
            final_state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            fallback_item = final_state["items"][fixture["items"][1]["queue_id"]]

        self.assertEqual(result.live_fallback_count, 1)
        self.assertEqual(
            (fallback_item["status"], fallback_item["review_status"]),
            ("live_fallback", "live_fallback"),
        )
        self.assertNotIn("path", fallback_item)

    def test_named_missing_voice_fallback_survives_final_pack_validation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(
                root / "named-fallback",
                names=("one",),
                named_narrator_fallback="Poacher I",
                narrator_selection_character="Hero",
            )
            queue_id = fixture["items"][0]["queue_id"]
            review_generation_item(fixture["state"], queue_id, "approved")

            result = publish(fixture, root / "final-pack")
            pack = load_game_pack(result.manifest)
            generated = json.loads(
                pack.generated_audio.path.read_text(encoding="utf-8")
            )["entries"][0]

        self.assertEqual(generated["requested_voice_character"], "Poacher I")
        self.assertEqual(generated["speaker"], "Poacher I")
        self.assertEqual(generated["voice_character"], "Narrator")
        self.assertEqual(generated["narrator_character"], "Hero")
        self.assertEqual(
            generated["synthesis_fallback"]["kind"],
            "missing_voice_to_narrator",
        )
        self.assertEqual(
            pack.extensions["vntts.authoring"]["narrator_selection"]["character"],
            "Hero",
        )

    def test_legacy_unknown_speaker_requires_role_bound_narrator_provenance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(
                root / "valid",
                names=("one",),
                legacy_narrator=True,
                narrator_selection_character="Hero",
            )
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "approved"
            )

            result = publish(fixture, root / "valid-pack")
            pack = load_game_pack(result.manifest)
            state = json.loads(fixture["state"].read_text(encoding="utf-8"))

            self.assertEqual(
                state["items"][fixture["items"][0]["queue_id"]]["voice_character"],
                "Narrator",
            )
            self.assertEqual(
                pack.extensions["vntts.authoring"]["narrator_selection"],
                {
                    "character": "Hero",
                    "reference_sha256": sha256_file(fixture["reference"]),
                },
            )

            missing = prepare_authoring_fixture(
                root / "missing",
                names=("one",),
                legacy_narrator=True,
            )
            review_generation_item(
                missing["state"], missing["items"][0]["queue_id"], "approved"
            )
            with self.assertRaisesRegex(
                FinalGamePackError, "role-bound narrator selection"
            ):
                publish(missing, root / "missing-pack")

            misbound = prepare_authoring_fixture(
                root / "misbound",
                names=("one",),
                legacy_narrator=True,
                narrator_selection_character="Missing Character",
            )
            review_generation_item(
                misbound["state"], misbound["items"][0]["queue_id"], "approved"
            )
            with self.assertRaisesRegex(FinalGamePackError, "not role-bound"):
                publish(misbound, root / "misbound-pack")

            self.assertFalse((root / "missing-pack").exists())
            self.assertFalse((root / "misbound-pack").exists())

    def test_deliberate_voice_override_requires_and_preserves_state_controls(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root)
            original_voice_sha256 = sha256_file(fixture["voices"])
            replacement_root = root / "replacement"
            replacement_reference = replacement_root / "references" / "hero.wav"
            write_pcm16_wav(replacement_reference, audio_samples() * 0.5, 16_000)
            replacement_manifest = replacement_root / "voice-manifest.json"
            write_voice_manifest(
                replacement_manifest,
                {
                    "version": 2,
                    "voices": [
                        {
                            "character": "Hero",
                            "speaker": "replacement-hero",
                            "references": ["references/hero.wav"],
                        }
                    ],
                },
            )
            shutil.rmtree(fixture["output"])
            generated = run_bulk_generation(
                fixture["queue"],
                fixture["output"],
                SyntheticRenderer(),
                provider="synthetic",
                model="synthetic-v1",
                generation_profile="stable",
                control_files={
                    "voice_manifest": replacement_manifest,
                    "voice_reference:0001": replacement_reference,
                },
            )
            fixture["state"] = generated.state
            fixture["manifest"] = generated.manifest
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "approved"
            )
            review_generation_item(
                fixture["state"], fixture["items"][1]["queue_id"], "rejected"
            )

            result = publish(
                fixture,
                root / "final-pack",
                voice_manifest_path=replacement_manifest,
            )
            pack = load_game_pack(result.manifest)
            provenance = pack.extensions["vntts.authoring"]
            replacement_voice_sha256 = sha256_file(replacement_manifest)

        self.assertTrue(provenance["voice_manifest_override"])
        self.assertEqual(
            provenance["queue_voice_manifest_sha256"], original_voice_sha256
        )
        self.assertEqual(
            provenance["selected_voice_manifest_sha256"],
            replacement_voice_sha256,
        )

    def test_publishes_approved_projection_without_mutating_authoring_sources(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root)
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "approved"
            )
            review_generation_item(
                fixture["state"], fixture["items"][1]["queue_id"], "rejected"
            )
            write_generated_audio_manifest(
                fixture["manifest"],
                {
                    "game": "Synthetic Game",
                    "language": "en",
                    "source_queue_sha256": sha256_file(fixture["queue"]),
                    "generated_at": "2026-08-16T12:04:00+00:00",
                },
                [],
            )
            source_paths = (
                fixture["story"],
                fixture["voices"],
                fixture["reference"],
                fixture["queue"],
                fixture["state"],
                fixture["manifest"],
                *fixture["output"].glob("audio/**/*.wav"),
            )
            before = {path: path.read_bytes() for path in source_paths}

            result = publish(fixture, root / "final-pack")

            pack = load_game_pack(result.manifest)
            generated = GeneratedAudioIndex.load(pack.generated_audio.path)
            generated_document = json.loads(
                pack.generated_audio.path.read_text(encoding="utf-8")
            )
            imported = import_game_pack(result.manifest)
            self.assertEqual(result.approved_count, 1)
            self.assertEqual(result.rejected_count, 1)
            self.assertEqual(len(generated.entries), 1)
            self.assertIn(
                "synthesis_provenance_sha256", generated_document["entries"][0]
            )
            self.assertEqual(
                generated.entries[0].line_id, fixture["items"][0]["line_id"]
            )
            self.assertEqual(imported.pack.game_id, "synthetic-game")
            self.assertEqual(
                pack.extensions["vntts.authoring"]["source_state_sha256"],
                sha256_file(fixture["state"]),
            )
            self.assertEqual(before, {path: path.read_bytes() for path in source_paths})
            self.assertFalse(list(root.glob(".final-pack.staging-*")))

    def test_refuses_pending_failed_or_active_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root, names=("one",))
            with self.assertRaisesRegex(FinalGamePackError, "pending review"):
                publish(fixture, root / "pending-pack")

            state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            state["items"][fixture["items"][0]["queue_id"]] = {
                "status": "failed",
                "attempts": 1,
                "seed": 0,
                "last_error": "failed",
                "updated_at": "2026-08-16T12:05:00+00:00",
            }
            fixture["state"].write_text(
                json.dumps(state, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(FinalGamePackError, "failed item"):
                publish(fixture, root / "failed-pack")

            state["items"] = {}
            state["active"] = {
                "queue_id": fixture["items"][0]["queue_id"],
                "line_id": fixture["items"][0]["line_id"],
                "text": fixture["items"][0]["text"],
                "phase": "generating",
                "attempt": 1,
                "attempt_limit": 1,
                "total_attempts": 1,
                "seed": 0,
            }
            fixture["state"].write_text(
                json.dumps(state, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(FinalGamePackError, "active or interrupted"):
                publish(fixture, root / "active-pack")

    def test_refuses_missing_selected_queue_item(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root)
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "approved"
            )
            review_generation_item(
                fixture["state"], fixture["items"][1]["queue_id"], "rejected"
            )
            state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            del state["items"][fixture["items"][1]["queue_id"]]
            fixture["state"].write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaisesRegex(FinalGamePackError, "missing 1 selected"):
                publish(fixture, root / "final-pack")

            self.assertFalse((root / "final-pack").exists())

    def test_existing_destination_is_never_overwritten(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root, names=("one",))
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "rejected"
            )
            destination = root / "final-pack"
            destination.mkdir()
            sentinel = destination / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")

            with self.assertRaisesRegex(FinalGamePackError, "already exists"):
                publish(fixture, destination)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_destination_created_during_staging_wins_without_overwrite(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root, names=("one",))
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "rejected"
            )
            destination = root / "final-pack"
            real_write = game_pack_module.write_game_pack

            def create_destination(*args, **kwargs):
                result = real_write(*args, **kwargs)
                destination.mkdir()
                (destination / "keep.txt").write_text("preserve", encoding="utf-8")
                return result

            real_exists = game_pack_module._path_exists

            def hide_destination(path):
                if Path(path) == destination:
                    return False
                return real_exists(path)

            with (
                patch.object(
                    game_pack_module, "write_game_pack", side_effect=create_destination
                ),
                patch.object(
                    game_pack_module, "_path_exists", side_effect=hide_destination
                ),
            ):
                with self.assertRaisesRegex(FinalGamePackError, "already exists"):
                    publish(fixture, destination)

            self.assertEqual(
                (destination / "keep.txt").read_text(encoding="utf-8"), "preserve"
            )
            self.assertFalse((destination / "game-pack.json").exists())
            self.assertFalse(list(root.glob(".final-pack.staging-*")))

    def test_source_mutation_during_staging_aborts_and_cleans_stage(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root, names=("one",))
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "rejected"
            )
            real_write = game_pack_module.write_game_pack

            def mutate_story(*args, **kwargs):
                result = real_write(*args, **kwargs)
                with fixture["story"].open("ab") as stream:
                    stream.write(b"\n")
                return result

            with patch.object(
                game_pack_module, "write_game_pack", side_effect=mutate_story
            ):
                with self.assertRaisesRegex(FinalGamePackError, "changed"):
                    publish(fixture, root / "final-pack")

            self.assertFalse((root / "final-pack").exists())
            self.assertFalse(list(root.glob(".final-pack.staging-*")))

    def test_state_mutation_after_snapshot_cannot_change_projection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root, names=("one",))
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "approved"
            )
            real_copy = game_pack_module._copy_control
            mutated = False

            def mutate_state(*args, **kwargs):
                nonlocal mutated
                result = real_copy(*args, **kwargs)
                if not mutated:
                    mutated = True
                    state = json.loads(fixture["state"].read_text(encoding="utf-8"))
                    item = state["items"][fixture["items"][0]["queue_id"]]
                    item["status"] = "generated"
                    item["review_status"] = "rejected"
                    fixture["state"].write_text(json.dumps(state), encoding="utf-8")
                return result

            with patch.object(
                game_pack_module, "_copy_control", side_effect=mutate_state
            ):
                with self.assertRaisesRegex(FinalGamePackError, "source changed"):
                    publish(fixture, root / "final-pack")

            self.assertFalse((root / "final-pack").exists())

    def test_publication_lease_tamper_aborts_before_commit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root, names=("one",))
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "rejected"
            )
            real_write = game_pack_module.write_game_pack

            def replace_owner(*args, **kwargs):
                result = real_write(*args, **kwargs)
                lease = root / ".final-pack.publication.json"
                document = json.loads(lease.read_text(encoding="utf-8"))
                document["owner"] = "successor"
                lease.write_text(json.dumps(document), encoding="utf-8")
                return result

            with patch.object(
                game_pack_module, "write_game_pack", side_effect=replace_owner
            ):
                with self.assertRaisesRegex(FinalGamePackError, "ownership was lost"):
                    publish(fixture, root / "final-pack")

            self.assertFalse((root / "final-pack").exists())

    def test_post_commit_lease_cleanup_ambiguity_does_not_report_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root, names=("one",))
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "rejected"
            )
            real_rename = game_pack_module._rename_directory_no_replace

            def rename_then_replace_leases(source, destination):
                real_rename(source, destination)
                publication_lease = root / ".final-pack.publication.json"
                publication = json.loads(publication_lease.read_text(encoding="utf-8"))
                publication["owner"] = "successor"
                publication_lease.write_text(json.dumps(publication), encoding="utf-8")
                generation_lease = fixture["output"] / ".generation-lease.json"
                generation = json.loads(generation_lease.read_text(encoding="utf-8"))
                generation["lease_id"] = "successor"
                generation_lease.write_text(json.dumps(generation), encoding="utf-8")

            with patch.object(
                game_pack_module,
                "_rename_directory_no_replace",
                side_effect=rename_then_replace_leases,
            ):
                result = publish(fixture, root / "final-pack")

            self.assertTrue(result.manifest.is_file())
            self.assertEqual(load_game_pack(result.manifest).game_id, "synthetic-game")

    def test_rejects_voice_reference_symlink_escape(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root, names=("one",))
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "rejected"
            )
            outside = root / "outside.wav"
            write_pcm16_wav(outside, audio_samples(), 16_000)
            fixture["reference"].unlink()
            fixture["reference"].symlink_to(outside)

            with self.assertRaisesRegex(FinalGamePackError, "leaves its source root"):
                publish(fixture, root / "final-pack")

            self.assertFalse((root / "final-pack").exists())

    def test_rejects_voice_reference_changed_after_synthesis(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root, names=("one",))
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "approved"
            )
            write_pcm16_wav(fixture["reference"], audio_samples() * 0.5, 16_000)

            with self.assertRaisesRegex(FinalGamePackError, "synthesis controls"):
                publish(fixture, root / "final-pack")

            self.assertFalse((root / "final-pack").exists())

    def test_rejects_unbound_queue_and_state_without_control_inventory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            unbound = prepare_authoring_fixture(
                root / "unbound", names=("one",), bind_sources=False
            )
            review_generation_item(
                unbound["state"], unbound["items"][0]["queue_id"], "rejected"
            )
            with self.assertRaisesRegex(FinalGamePackError, "source path binding"):
                publish(unbound, root / "unbound-pack")

            fixture = prepare_authoring_fixture(root / "no-controls", names=("one",))
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "approved"
            )
            state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            del state["synthesis_controls"]
            fixture["state"].write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(FinalGamePackError, "per-control"):
                publish(fixture, root / "no-controls-pack")

    def test_queue_rejects_different_story_checksum(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root, names=("one",))
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "rejected"
            )
            replacement = root / "replacement-story.jsonl"
            rows = fixture["story"].read_text(encoding="utf-8").splitlines()
            metadata = json.loads(rows[0])
            metadata["generated_at"] = "2026-08-16T12:01:00+00:00"
            replacement.write_text(
                "\n".join([json.dumps(metadata, sort_keys=True), *rows[1:]]) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                FinalGamePackError, "story index (path|checksum)"
            ):
                publish(
                    fixture,
                    root / "final-pack",
                    story_index_path=replacement,
                )

    def test_rejects_bound_story_changed_only_for_rejected_line(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root)
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "approved"
            )
            review_generation_item(
                fixture["state"], fixture["items"][1]["queue_id"], "rejected"
            )
            rows = fixture["story"].read_text(encoding="utf-8").splitlines()
            rejected = json.loads(rows[2])
            rejected["chapter"] = "altered-unapproved-chapter"
            rows[2] = json.dumps(rejected, sort_keys=True)
            fixture["story"].write_text("\n".join(rows) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(FinalGamePackError, "story index checksum"):
                publish(fixture, root / "final-pack")

            self.assertFalse((root / "final-pack").exists())

    def test_cli_publishes_and_reports_verified_pack(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = prepare_authoring_fixture(root, names=("one",))
            review_generation_item(
                fixture["state"], fixture["items"][0]["queue_id"], "approved"
            )
            destination = root / "final-pack"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = authoring_main(
                    [
                        "publish-pack",
                        "--state",
                        str(fixture["state"]),
                        "--queue",
                        str(fixture["queue"]),
                        "--story-index",
                        str(fixture["story"]),
                        "--voice-manifest",
                        str(fixture["voices"]),
                        "--output",
                        str(destination),
                        "--game-id",
                        "synthetic-game",
                        "--game-version",
                        "1.0",
                        "--producer",
                        "synthetic=1.0",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["approved_count"], 1)
            self.assertEqual(load_game_pack(payload["manifest"]).game_version, "1.0")


if __name__ == "__main__":
    unittest.main()

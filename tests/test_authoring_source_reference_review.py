import hashlib
import json
import struct
import unittest
import zlib
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from vntts_artifacts import (
    VoiceGenerationQueue,
    expected_voice_generation_queue_id,
    write_story_index_document,
    write_voice_generation_queue,
)
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.voice_manifest import load_voice_manifest, write_voice_manifest

from vntts.authoring.bulk_generation import load_generation_state, run_bulk_generation
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.listening import (
    create_listening_session_from_reports,
    load_listening_session,
)
from vntts.authoring.portrait_aliases import (
    PortraitAliasError,
    build_portrait_alias_decision,
    build_portrait_alias_plan,
    load_portrait_alias_decision,
    load_portrait_alias_plan,
    portrait_identity_by_variant,
    write_portrait_alias_decision,
    write_portrait_alias_plan,
)
from vntts.authoring.source_reference_bindings import (
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
    queue_voice_overrides_sha256,
)
from vntts.authoring.source_reference_quality import (
    SourceReferenceQualityError,
    accepted_source_reference_variants,
    load_source_reference_quality_review,
    publish_source_reference_quality_review,
    quality_review_progress,
    record_source_reference_quality_decision,
)
from vntts.authoring.source_reference_review import (
    REFERENCE_PLAN_SCHEMA,
    SourceReferenceReviewError,
    import_source_reference_review,
    load_source_reference_plan,
    publish_source_reference_bindings,
    publish_source_reference_evaluation,
    publish_source_reference_listening_reports,
)
from vntts.synthesis import (
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisResult,
    SynthesisTiming,
)


class EvaluationRenderer:
    name = "synthetic"
    model_name = "synthetic-v1"

    def __init__(self):
        self.requests = []

    def render(self, request):
        self.requests.append(request)
        pcm = np.sin(np.linspace(0, 20, 4_000, dtype=np.float32)) * 0.2

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

    def stop(self):
        pass


def write_test_png(path, *, red):
    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    payload = b"\x89PNG\r\n\x1a\n"
    payload += chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
    row = b"\x00" + bytes((red, 40, 20, 255)) * 2
    payload += chunk(b"IDAT", zlib.compress(row * 2))
    payload += chunk(b"IEND", b"")
    path.write_bytes(payload)


def candidate_key(character, portrait, bank, media_id, reference_sha256):
    identity = json.dumps(
        [character, portrait, bank, media_id, reference_sha256],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()


class AuthoringSourceReferenceReviewTest(unittest.TestCase):
    def publish_quality_fixture(
        self, root, *, portrait_directory=None, shared_portrait_bank=False
    ):
        report, review, story = self.write_inputs(
            root, shared_portrait_bank=shared_portrait_bank
        )
        plan = import_source_reference_review(report, review, story, root / "plan")
        evaluation = publish_source_reference_evaluation(
            plan.directory, root / "evaluation"
        )
        generation = run_bulk_generation(
            evaluation.directory / "queue.jsonl",
            root / "generation",
            EvaluationRenderer(),
            provider="synthetic",
            model="synthetic-v1",
            generation_profile="stable",
        )
        quality = publish_source_reference_quality_review(
            plan.directory,
            evaluation.directory,
            generation.state,
            root / "quality",
            portrait_directory=portrait_directory,
        )
        return plan, evaluation, generation, quality

    def write_inputs(self, root, *, shared_portrait_bank=False):
        root = Path(root)
        references = root / "references"
        references.mkdir()
        candidates = []
        decisions = []
        accepted_young_bank = (
            "hero-adult.bnk" if shared_portrait_bank else "hero-young.bnk"
        )
        for index, (portrait, bank, decision) in enumerate(
            (
                ("adult.png", "hero-adult.bnk", "accept"),
                ("young.png", accepted_young_bank, "accept"),
                ("adult.png", "hero-adult.bnk", "reject"),
            ),
            start=1,
        ):
            reference = references / f"{index}.wav"
            values = np.sin(np.linspace(0, 20 + index, 4_000, dtype=np.float32)) * 0.2
            write_pcm16_wav(reference, values, 16_000)
            reference_sha256 = hashlib.sha256(reference.read_bytes()).hexdigest()
            candidate = {
                "character": "Hero",
                "portrait": portrait,
                "source_bank": bank,
                "media_id": index,
                "reference": f"references/{index}.wav",
                "reference_sha256": reference_sha256,
                "technical_pass": True,
                "transcript_conflict": False,
                "source_lines": [
                    {
                        "line_id": f"source:{index}",
                        "text": f"Source transcript {index}",
                    }
                ],
            }
            evidence_sha256 = hashlib.sha256(
                json.dumps(
                    candidate,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            key = candidate_key("Hero", portrait, bank, index, reference_sha256)
            candidates.append(candidate)
            decisions.append(
                {
                    "candidate_key": key,
                    "candidate_evidence_sha256": evidence_sha256,
                    "reference_sha256": reference_sha256,
                    "decision": decision,
                    "notes": "exact human decision",
                }
            )
        report = root / "report.json"
        report.write_text(
            json.dumps(
                {
                    "schema": "r1999.story-voice-reference-candidates",
                    "schema_version": 1,
                    "groups": [],
                    "candidates": candidates,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        review = root / "review.json"
        review.write_text(
            json.dumps(
                {
                    "schema": "r1999.story-voice-reference-review",
                    "schema_version": 2,
                    "candidate_report_sha256": hashlib.sha256(
                        report.read_bytes()
                    ).hexdigest(),
                    "decisions": decisions,
                    "invalidated_decisions": [],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        records = []
        for index, portrait in enumerate(
            ("adult.png", "young.png", "other.png"), start=1
        ):
            text = f"Missing target {index}."
            records.append(
                {
                    "record_type": "line",
                    "line_id": f"target:{index}",
                    "chapter": "one",
                    "sequence": index,
                    "speaker": "Hero",
                    "voice_character": "Hero",
                    "text": text,
                    "text_sha256": text_sha256(text),
                    "kind": "dialogue",
                    "source_audio_status": "absent",
                    "source_audio_reason": "fixture",
                    "source_kind": "story",
                    "speakable": True,
                    "collection_id": "main",
                    "portrait": portrait,
                }
            )
        story = root / "story.jsonl"
        write_story_index_document(
            story,
            {
                "game": "Synthetic",
                "language": "en",
                "generated_at": "2026-08-18T00:00:00+00:00",
                "collections": [
                    {
                        "collection_id": "main",
                        "title": "Main",
                        "kind": "story",
                        "order": 1,
                    }
                ],
            },
            records,
        )
        return report, review, story

    def test_v2_unrouted_media_uses_fixed_corpus_without_invented_transcript(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            report_document = json.loads(report.read_text(encoding="utf-8"))
            report_document["schema_version"] = 2
            for index, candidate in enumerate(report_document["candidates"], start=1):
                candidate["candidate_origin"] = "exact_bank_unrouted_media"
                candidate["source_event_ids"] = [10_000 + index]
                candidate["source_lines"] = []
            report.write_text(
                json.dumps(report_document, sort_keys=True), encoding="utf-8"
            )
            review_document = json.loads(review.read_text(encoding="utf-8"))
            review_document["candidate_report_sha256"] = hashlib.sha256(
                report.read_bytes()
            ).hexdigest()
            evidence_by_key = {}
            for candidate in report_document["candidates"]:
                key = candidate_key(
                    candidate["character"],
                    candidate["portrait"],
                    candidate["source_bank"],
                    candidate["media_id"],
                    candidate["reference_sha256"],
                )
                evidence_by_key[key] = hashlib.sha256(
                    json.dumps(
                        candidate,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            for decision in review_document["decisions"]:
                decision["candidate_evidence_sha256"] = evidence_by_key[
                    decision["candidate_key"]
                ]
            review.write_text(
                json.dumps(review_document, sort_keys=True), encoding="utf-8"
            )

            plan = import_source_reference_review(report, review, story, root / "plan")
            plan_document = load_source_reference_plan(plan.directory)
            evaluation = publish_source_reference_evaluation(
                plan.directory, root / "evaluation"
            )
            comparison = json.loads(
                (evaluation.directory / "comparison.json").read_text(encoding="utf-8")
            )
            generation = run_bulk_generation(
                evaluation.directory / "queue.jsonl",
                root / "generation",
                EvaluationRenderer(),
                provider="synthetic",
                model="synthetic-v1",
                generation_profile="stable",
            )
            quality = publish_source_reference_quality_review(
                plan.directory,
                evaluation.directory,
                generation.state,
                root / "quality",
            )
            session = load_source_reference_quality_review(quality.session)

            self.assertEqual(evaluation.queue_items, 6)
            self.assertTrue(
                all(
                    reference["candidate_origin"] == "exact_bank_unrouted_media"
                    and not reference["source_transcripts"]
                    and reference["source_event_ids"]
                    for cluster in plan_document["clusters"]
                    for reference in cluster["references"]
                )
            )
            self.assertTrue(
                all(
                    "source_match_queue_id" not in value
                    for value in comparison["variants"]
                )
            )
            self.assertTrue(
                all(
                    len(card["generated_samples"]) == 3
                    and {
                        sample["evaluation_kind"]
                        for sample in card["generated_samples"]
                    }
                    == {"fixed-1", "fixed-2", "fixed-3"}
                    for card in session["variants"]
                )
            )
            with self.assertRaisesRegex(
                SourceReferenceReviewError, "fixed-corpus.*unrouted media"
            ):
                publish_source_reference_listening_reports(
                    evaluation.directory,
                    generation.state,
                    root / "listening",
                )

    def write_base_voice_manifest(self, root, *, include_rhiannon=False):
        reference = root / "base-references" / "centurion.wav"
        reference.parent.mkdir()
        values = np.sin(np.linspace(0, 24, 4_000, dtype=np.float32)) * 0.2
        write_pcm16_wav(reference, values, 16_000)
        manifest = root / "base-voice-manifest.json"
        voices = [
            {
                "character": "Centurion",
                "speaker": "centurion",
                "references": ["base-references/centurion.wav"],
            }
        ]
        if include_rhiannon:
            rhiannon_reference = root / "base-references" / "rhiannon.wav"
            values = np.sin(np.linspace(0, 18, 5_000, dtype=np.float32)) * 0.2
            write_pcm16_wav(rhiannon_reference, values, 16_000)
            voices.append(
                {
                    "character": "Rhiannon",
                    "speaker": "rhiannon",
                    "aliases": ["Aderyn (adult)"],
                    "references": ["base-references/rhiannon.wav"],
                }
            )
        write_voice_manifest(
            manifest,
            {
                "version": 2,
                "game": "Synthetic",
                "language": "en",
                "voices": voices,
            },
        )
        return manifest

    def test_imports_self_contained_variant_clusters_and_exact_queue_ids(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            source_hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (report, review, story)
            }

            result = import_source_reference_review(
                report, review, story, root / "imported-plan"
            )
            plan = load_source_reference_plan(result.directory)

            after_hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (report, review, story)
            }

        self.assertEqual(plan["schema"], REFERENCE_PLAN_SCHEMA)
        self.assertEqual(result.accepted_clusters, 2)
        self.assertEqual(result.accepted_candidates, 2)
        self.assertEqual(result.mapped_queue_items, 2)
        self.assertEqual(source_hashes, after_hashes)
        clusters = {item["portrait"]: item for item in plan["clusters"]}
        self.assertEqual(set(clusters), {"adult.png", "young.png"})
        self.assertEqual(
            clusters["adult.png"]["queue_items"][0]["queue_id"],
            expected_voice_generation_queue_id(
                "target:1", text_sha256("Missing target 1.")
            ),
        )
        self.assertNotEqual(
            clusters["adult.png"]["cluster_id"], clusters["young.png"]["cluster_id"]
        )
        self.assertEqual(len(plan["fixed_evaluation_corpus"]), 3)

    def test_rejects_changed_reference_and_never_creates_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            (root / "references/1.wav").write_bytes(b"replacement")
            output = root / "unsafe"

            with self.assertRaisesRegex(SourceReferenceReviewError, "checksum changed"):
                import_source_reference_review(report, review, story, output)

            self.assertFalse(output.exists())

    def test_plan_loader_rejects_tampered_copied_reference(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            result = import_source_reference_review(
                report, review, story, root / "imported-plan"
            )
            plan = json.loads((result.directory / "plan.json").read_text())
            relative = plan["clusters"][0]["references"][0]["path"]
            (result.directory / relative).write_bytes(b"tampered")

            with self.assertRaisesRegex(SourceReferenceReviewError, "changed"):
                load_source_reference_plan(result.directory)

    def test_refuses_to_replace_existing_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            output = root / "exists"
            output.mkdir()

            with self.assertRaisesRegex(SourceReferenceReviewError, "output exists"):
                import_source_reference_review(report, review, story, output)

    def test_cli_publishes_machine_readable_summary(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = authoring_main(
                    [
                        "import-reference-review",
                        "--report",
                        str(report),
                        "--review",
                        str(review),
                        "--story-index",
                        str(story),
                        "--output",
                        str(root / "plan"),
                    ]
                )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["accepted_clusters"], 2)
        self.assertEqual(payload["mapped_queue_items"], 2)

    def test_publishes_fixed_corpus_inputs_for_every_accepted_variant(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            plan = import_source_reference_review(report, review, story, root / "plan")

            result = publish_source_reference_evaluation(
                plan.directory, root / "evaluation"
            )
            queue = VoiceGenerationQueue.load(result.directory / "queue.jsonl")
            _manifest, voices = load_voice_manifest(
                result.directory / "voice-manifest.json", allow_legacy=False
            )
            comparison = json.loads(
                (result.directory / "comparison.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result.variants, 2)
        self.assertEqual(result.queue_items, 8)
        self.assertEqual(len(queue.items), 8)
        self.assertEqual(len(voices), 2)
        self.assertEqual(
            {
                item.text
                for item in queue.items
                if item.document["evaluation_kind"] == "source-match"
            },
            {"Source transcript 1", "Source transcript 2"},
        )
        self.assertEqual(
            [item["affected_queue_item_count"] for item in comparison["variants"]],
            [1, 1],
        )
        self.assertTrue(
            all(item["manual_blind_review_required"] for item in comparison["variants"])
        )

    def test_evaluation_refuses_overwrite_and_tampered_plan_reference(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            plan = import_source_reference_review(report, review, story, root / "plan")
            output = root / "evaluation"
            publish_source_reference_evaluation(plan.directory, output)

            with self.assertRaisesRegex(SourceReferenceReviewError, "output exists"):
                publish_source_reference_evaluation(plan.directory, output)

            document = json.loads((plan.directory / "plan.json").read_text())
            reference = document["clusters"][0]["references"][0]["path"]
            (plan.directory / reference).write_bytes(b"changed")
            with self.assertRaisesRegex(SourceReferenceReviewError, "changed"):
                publish_source_reference_evaluation(
                    plan.directory, root / "unsafe-evaluation"
                )

    def test_cli_builds_reference_evaluation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            plan = import_source_reference_review(report, review, story, root / "plan")
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = authoring_main(
                    [
                        "build-reference-evaluation",
                        "--plan",
                        str(plan.directory),
                        "--output",
                        str(root / "evaluation"),
                    ]
                )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["variants"], 2)
        self.assertEqual(payload["queue_items"], 8)

    def test_publishes_reports_and_creates_final_blind_session(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            plan = import_source_reference_review(report, review, story, root / "plan")
            evaluation = publish_source_reference_evaluation(
                plan.directory, root / "evaluation"
            )
            generation = run_bulk_generation(
                evaluation.directory / "queue.jsonl",
                root / "generation",
                EvaluationRenderer(),
                provider="synthetic",
                model="synthetic-v1",
                generation_profile="stable",
            )

            reports = publish_source_reference_listening_reports(
                evaluation.directory, generation.state, root / "reports"
            )
            session_path = create_listening_session_from_reports(
                sorted(reports.directory.glob("*.json")), root / "session", seed=9
            )
            session = load_listening_session(session_path)
            public_session = session_path.read_text(encoding="utf-8")

        self.assertEqual(reports.reports, 3)
        self.assertEqual(reports.samples, 10)
        self.assertEqual(reports.blind_trials, 5)
        self.assertEqual(session["trial_count"], 5)
        self.assertNotIn("generated:", public_session)

    def test_listening_report_cli_refuses_to_replace_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            plan = import_source_reference_review(report, review, story, root / "plan")
            evaluation = publish_source_reference_evaluation(
                plan.directory, root / "evaluation"
            )
            generation = run_bulk_generation(
                evaluation.directory / "queue.jsonl",
                root / "generation",
                EvaluationRenderer(),
                provider="synthetic",
                model="synthetic-v1",
                generation_profile="stable",
            )
            output = root / "reports"
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = authoring_main(
                    [
                        "build-reference-listening-reports",
                        "--evaluation",
                        str(evaluation.directory),
                        "--state",
                        str(generation.state),
                        "--output",
                        str(output),
                    ]
                )

            with self.assertRaisesRegex(SourceReferenceReviewError, "output exists"):
                publish_source_reference_listening_reports(
                    evaluation.directory, generation.state, output
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["blind_trials"], 5)

    def test_publishes_cluster_quality_cards_and_records_distinct_decisions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _plan, _evaluation, _generation, result = self.publish_quality_fixture(root)
            session = load_source_reference_quality_review(result.session)
            first, second = session["variants"]
            record_source_reference_quality_decision(
                result.session, first["variant_id"], "accept"
            )
            updated = record_source_reference_quality_decision(
                result.session, second["variant_id"], "needs_sample"
            )

        self.assertEqual(result.variants, 2)
        self.assertEqual(result.generated_samples, 8)
        self.assertEqual(quality_review_progress(updated), (2, 2))
        self.assertEqual(
            accepted_source_reference_variants(updated), (first["variant_id"],)
        )
        self.assertTrue(all(card["reference"]["audio"] for card in session["variants"]))
        self.assertTrue(all(card["generated_samples"] for card in session["variants"]))

    def test_quality_review_copies_and_validates_available_portraits(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            portraits = root / "portraits"
            portraits.mkdir()
            write_test_png(portraits / "adult.png", red=120)
            write_test_png(portraits / "young.png", red=200)
            _plan, _evaluation, _generation, result = self.publish_quality_fixture(
                root, portrait_directory=portraits
            )
            session = load_source_reference_quality_review(result.session)

            copied = [card["portrait_image"] for card in session["variants"]]
            self.assertTrue(all(record["width"] == 2 for record in copied))
            copied[0]["width"] = 99
            (result.session).write_text(
                json.dumps(session, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                SourceReferenceQualityError, "portrait metadata changed"
            ):
                load_source_reference_quality_review(result.session)

    def test_portrait_alias_requires_same_voice_evidence_and_human_decision(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            portraits = root / "portraits"
            portraits.mkdir()
            write_test_png(portraits / "adult.png", red=120)
            write_test_png(portraits / "young.png", red=121)
            (root / "separate").mkdir()
            _plan, _evaluation, _generation, separate = self.publish_quality_fixture(
                root / "separate",
                portrait_directory=portraits,
            )
            separate_session = load_source_reference_quality_review(separate.session)
            for card in separate_session["variants"]:
                record_source_reference_quality_decision(
                    separate.session, card["variant_id"], "accept"
                )
            self.assertEqual(
                build_portrait_alias_plan(separate.session).document["suggestions"],
                [],
            )

            shared_root = root / "shared"
            shared_root.mkdir()
            _plan, _evaluation, _generation, shared = self.publish_quality_fixture(
                shared_root,
                portrait_directory=portraits,
                shared_portrait_bank=True,
            )
            shared_session = load_source_reference_quality_review(shared.session)
            for card in shared_session["variants"]:
                record_source_reference_quality_decision(
                    shared.session, card["variant_id"], "accept"
                )
            plan = build_portrait_alias_plan(shared.session)
            plan_path = write_portrait_alias_plan(plan, root / "aliases-plan.json")
            loaded_plan = load_portrait_alias_plan(plan_path)
            suggestion = loaded_plan.document["suggestions"][0]
            decision = build_portrait_alias_decision(
                loaded_plan, [suggestion["suggestion_id"]]
            )
            decision_path = write_portrait_alias_decision(
                decision, root / "aliases-decision.json"
            )
            loaded_decision = load_portrait_alias_decision(decision_path, loaded_plan)
            identities = portrait_identity_by_variant(loaded_decision)

        self.assertEqual(plan.document["suggestion_count"], 1)
        self.assertEqual(suggestion["dhash_distance"], 0)
        self.assertEqual(len(identities), 2)
        self.assertEqual(len(set(identities.values())), 1)
        self.assertEqual(
            {value["portrait"] for value in suggestion["variants"]},
            {"adult.png", "young.png"},
        )

    def test_portrait_alias_plan_rejects_changed_portrait_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            portraits = root / "portraits"
            portraits.mkdir()
            write_test_png(portraits / "adult.png", red=120)
            write_test_png(portraits / "young.png", red=121)
            _plan, _evaluation, _generation, quality = self.publish_quality_fixture(
                root,
                portrait_directory=portraits,
                shared_portrait_bank=True,
            )
            session = load_source_reference_quality_review(quality.session)
            for card in session["variants"]:
                record_source_reference_quality_decision(
                    quality.session, card["variant_id"], "accept"
                )
            plan = build_portrait_alias_plan(quality.session)
            path = write_portrait_alias_plan(plan, root / "alias-plan.json")
            current = load_source_reference_quality_review(quality.session)
            portrait = (
                quality.directory / current["variants"][0]["portrait_image"]["image"]
            )
            portrait.write_bytes(b"changed")

            with self.assertRaisesRegex(PortraitAliasError, "changed"):
                load_portrait_alias_plan(path)

    def test_quality_review_rejects_tampered_audio_and_concurrent_decision(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _plan, _evaluation, _generation, result = self.publish_quality_fixture(root)
            session = load_source_reference_quality_review(result.session)
            variant = session["variants"][0]
            lock = result.session.with_name(f".{result.session.name}.lock")
            lock.write_text("other-reviewer", encoding="utf-8")
            with self.assertRaisesRegex(
                SourceReferenceQualityError, "Another source-reference decision"
            ):
                record_source_reference_quality_decision(
                    result.session, variant["variant_id"], "accept"
                )
            lock.unlink()
            audio = result.directory / variant["generated_samples"][0]["audio"]
            audio.write_bytes(b"tampered")

            with self.assertRaisesRegex(SourceReferenceQualityError, "changed"):
                load_source_reference_quality_review(result.session)

    def test_binding_cli_requires_completed_quality_review(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, _evaluation, _generation, result = self.publish_quality_fixture(root)
            manifest = self.write_base_voice_manifest(root, include_rhiannon=True)
            session = load_source_reference_quality_review(result.session)
            for card in session["variants"]:
                record_source_reference_quality_decision(
                    result.session, card["variant_id"], "accept"
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = authoring_main(
                    [
                        "build-reference-bindings",
                        "--plan",
                        str(plan.directory),
                        "--voice-manifest",
                        str(manifest),
                        "--narrator-character",
                        "Centurion",
                        "--include-base-character",
                        "Rhiannon",
                        "--quality-review",
                        str(result.session),
                        "--output",
                        str(root / "bindings"),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            manifest = json.loads(
                (root / "bindings/voice-manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["selected_variants"], 2)
        self.assertIn("Rhiannon", {voice["character"] for voice in manifest["voices"]})
        self.assertEqual(
            len(
                manifest["vntts.authoring.source_reference_bindings"][
                    "source_reference_quality_review_sha256"
                ]
            ),
            64,
        )

    def test_binding_publication_rejects_incomplete_quality_review(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, _evaluation, _generation, result = self.publish_quality_fixture(root)
            manifest = self.write_base_voice_manifest(root)

            with self.assertRaisesRegex(SourceReferenceReviewError, "incomplete"):
                publish_source_reference_bindings(
                    plan.directory,
                    manifest,
                    "Centurion",
                    None,
                    root / "bindings",
                    quality_review=result.session,
                )
            self.assertFalse((root / "bindings").exists())

    def test_binding_manifest_routes_exact_queue_ids_with_provenance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            base_manifest = self.write_base_voice_manifest(root)
            plan_result = import_source_reference_review(
                report, review, story, root / "plan"
            )
            plan = load_source_reference_plan(plan_result.directory)
            variants = [
                f"{cluster['cluster_id']}-anchor-1" for cluster in plan["clusters"]
            ]
            bindings = publish_source_reference_bindings(
                plan_result.directory,
                base_manifest,
                "Centurion",
                variants,
                root / "bindings",
            )
            manifest_document, voices = load_voice_manifest(
                bindings.directory / "voice-manifest.json", allow_legacy=False
            )
            queue_items = []
            for index in (1, 2):
                text = f"Missing target {index}."
                text_hash = text_sha256(text)
                queue_items.append(
                    {
                        "record_type": "generation_item",
                        "queue_id": expected_voice_generation_queue_id(
                            f"target:{index}", text_hash
                        ),
                        "line_id": f"target:{index}",
                        "text": text,
                        "text_sha256": text_hash,
                        "speaker": "Hero",
                        "voice_character": "Hero",
                        "action": "generate",
                    }
                )
            queue_path = root / "queue.jsonl"
            write_voice_generation_queue(
                queue_path,
                {"game": "Synthetic", "language": "en"},
                queue_items,
            )
            overrides = queue_voice_overrides_from_manifest(
                manifest_document,
                queue_ids=(item["queue_id"] for item in queue_items),
                voices=voices,
            )
            renderer = EvaluationRenderer()
            generation = run_bulk_generation(
                queue_path,
                root / "generated",
                renderer,
                provider="synthetic",
                model="synthetic-v1",
                generation_profile="stable",
                queue_voice_overrides=overrides,
            )
            state = load_generation_state(generation.state, queue_path)

        self.assertEqual(bindings.selected_variants, 2)
        self.assertEqual(bindings.bound_queue_items, 2)
        self.assertEqual(len(overrides), 2)
        self.assertEqual(
            {request.voice for request in renderer.requests}, set(overrides.values())
        )
        for queue_id, character in overrides.items():
            result = state["items"][queue_id]
            self.assertEqual(result["voice_character"], character)
            self.assertEqual(
                result["source_reference_binding"]["synthesis_voice_character"],
                character,
            )

    def test_binding_manifest_copies_only_explicit_base_characters(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            base_manifest = self.write_base_voice_manifest(root, include_rhiannon=True)
            plan_result = import_source_reference_review(
                report, review, story, root / "plan"
            )
            plan = load_source_reference_plan(plan_result.directory)
            variant = f"{plan['clusters'][0]['cluster_id']}-anchor-1"
            default_result = publish_source_reference_bindings(
                plan_result.directory,
                base_manifest,
                "Centurion",
                [variant],
                root / "default-bindings",
            )
            default_document, default_voices = load_voice_manifest(
                default_result.directory / "voice-manifest.json", allow_legacy=False
            )
            result = publish_source_reference_bindings(
                plan_result.directory,
                base_manifest,
                "Centurion",
                [variant],
                root / "included-bindings",
                base_characters=("Rhiannon",),
            )
            document, voices = load_voice_manifest(
                result.directory / "voice-manifest.json", allow_legacy=False
            )
            rhiannon = next(voice for voice in voices if voice.character == "Rhiannon")
            copied = result.directory / rhiannon.references[0]
            original = root / "base-references/rhiannon.wav"
            copied_payload = copied.read_bytes()
            original_payload = original.read_bytes()
            included_base_characters = document[
                "vntts.authoring.source_reference_bindings"
            ]["included_base_characters"]

        self.assertNotIn("Rhiannon", {voice.character for voice in default_voices})
        self.assertNotIn(
            "included_base_characters",
            default_document["vntts.authoring.source_reference_bindings"],
        )
        self.assertEqual(len(voices), 3)
        self.assertIn("Centurion", {voice.character for voice in voices})
        self.assertIn("Rhiannon", {voice.character for voice in voices})
        self.assertTrue(
            any(voice.character.startswith("Source reference") for voice in voices)
        )
        self.assertEqual(copied_payload, original_payload)
        self.assertEqual(
            included_base_characters,
            [
                {
                    "character": "Rhiannon",
                    "reference_sha256s": [hashlib.sha256(original_payload).hexdigest()],
                }
            ],
        )

    def test_binding_manifest_rejects_unknown_or_duplicate_base_character(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            base_manifest = self.write_base_voice_manifest(root, include_rhiannon=True)
            plan_result = import_source_reference_review(
                report, review, story, root / "plan"
            )
            plan = load_source_reference_plan(plan_result.directory)
            variant = f"{plan['clusters'][0]['cluster_id']}-anchor-1"

            with self.assertRaisesRegex(
                SourceReferenceReviewError, "has no references"
            ):
                publish_source_reference_bindings(
                    plan_result.directory,
                    base_manifest,
                    "Centurion",
                    [variant],
                    root / "unknown-bindings",
                    base_characters=("Missing",),
                )
            with self.assertRaisesRegex(SourceReferenceReviewError, "distinct"):
                publish_source_reference_bindings(
                    plan_result.directory,
                    base_manifest,
                    "Centurion",
                    [variant],
                    root / "duplicate-bindings",
                    base_characters=("Rhiannon", "rhiannon"),
                )
            self.assertFalse((root / "unknown-bindings").exists())
            self.assertFalse((root / "duplicate-bindings").exists())

    def test_binding_manifest_rejects_tampered_override_digest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            base_manifest = self.write_base_voice_manifest(root)
            plan_result = import_source_reference_review(
                report, review, story, root / "plan"
            )
            plan = load_source_reference_plan(plan_result.directory)
            variant = f"{plan['clusters'][0]['cluster_id']}-anchor-1"
            result = publish_source_reference_bindings(
                plan_result.directory,
                base_manifest,
                "Centurion",
                [variant],
                root / "bindings",
            )
            manifest, voices = load_voice_manifest(
                result.directory / "voice-manifest.json", allow_legacy=False
            )
            manifest["vntts.authoring.source_reference_bindings"][
                "queue_voice_overrides_sha256"
            ] = "0" * 64

            with self.assertRaisesRegex(SourceReferenceBindingError, "inconsistent"):
                queue_voice_overrides_from_manifest(manifest, voices=voices)

    def test_binding_manifest_rejects_unselected_manifest_voice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = self.write_inputs(root)
            base_manifest = self.write_base_voice_manifest(root)
            plan_result = import_source_reference_review(
                report, review, story, root / "plan"
            )
            plan = load_source_reference_plan(plan_result.directory)
            variant = f"{plan['clusters'][0]['cluster_id']}-anchor-1"
            result = publish_source_reference_bindings(
                plan_result.directory,
                base_manifest,
                "Centurion",
                [variant],
                root / "bindings",
            )
            manifest, voices = load_voice_manifest(
                result.directory / "voice-manifest.json", allow_legacy=False
            )
            bindings = manifest["vntts.authoring.source_reference_bindings"]
            queue_id = next(iter(bindings["queue_voice_overrides"]))
            bindings["queue_voice_overrides"][queue_id] = "Centurion"
            bindings["queue_voice_overrides_sha256"] = queue_voice_overrides_sha256(
                bindings["queue_voice_overrides"]
            )

            with self.assertRaisesRegex(
                SourceReferenceBindingError, "was not explicitly selected"
            ):
                queue_voice_overrides_from_manifest(manifest, voices=voices)


if __name__ == "__main__":
    unittest.main()

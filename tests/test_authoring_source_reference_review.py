import hashlib
import json
import unittest
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
from vntts.authoring.source_reference_bindings import (
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
    queue_voice_overrides_sha256,
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


def candidate_key(character, portrait, bank, media_id, reference_sha256):
    identity = json.dumps(
        [character, portrait, bank, media_id, reference_sha256],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()


class AuthoringSourceReferenceReviewTest(unittest.TestCase):
    def write_inputs(self, root):
        root = Path(root)
        references = root / "references"
        references.mkdir()
        candidates = []
        decisions = []
        for index, (portrait, bank, decision) in enumerate(
            (
                ("adult.png", "hero-adult.bnk", "accept"),
                ("young.png", "hero-young.bnk", "accept"),
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

    def write_base_voice_manifest(self, root):
        reference = root / "base-references" / "centurion.wav"
        reference.parent.mkdir()
        values = np.sin(np.linspace(0, 24, 4_000, dtype=np.float32)) * 0.2
        write_pcm16_wav(reference, values, 16_000)
        manifest = root / "base-voice-manifest.json"
        write_voice_manifest(
            manifest,
            {
                "version": 2,
                "game": "Synthetic",
                "language": "en",
                "voices": [
                    {
                        "character": "Centurion",
                        "speaker": "centurion",
                        "references": ["base-references/centurion.wav"],
                    }
                ],
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

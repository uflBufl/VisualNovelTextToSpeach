import hashlib
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts import (
    expected_voice_generation_queue_id,
    write_story_index_document,
)
from vntts_artifacts.hashing import text_sha256

from vntts.authoring.cli import main as authoring_main
from vntts.authoring.source_reference_review import (
    REFERENCE_PLAN_SCHEMA,
    SourceReferenceReviewError,
    import_source_reference_review,
    load_source_reference_plan,
)


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
            reference.write_bytes(f"RIFF exact reference {index}".encode())
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


if __name__ == "__main__":
    unittest.main()

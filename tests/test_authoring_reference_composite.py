import hashlib
import json
import math
import struct
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from vntts_artifacts import VoiceGenerationQueue
from vntts_artifacts.voice_manifest import load_voice_manifest

from vntts.authoring.bulk_generation import run_bulk_generation
from vntts.authoring.reference_composite import (
    COMPOSITE_SCHEMA,
    ReferenceCompositeError,
    publish_composite_quality_review,
    publish_exact_bank_reference_composite,
)
from vntts.authoring.source_reference_quality import (
    load_source_reference_quality_review,
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


class _Renderer:
    name = "synthetic"
    model_name = "synthetic-v1"

    def render(self, request):
        pcm = np.full(4_000, 0.1, dtype=np.float32)

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


class AuthoringReferenceCompositeTest(unittest.TestCase):
    def write_wav(self, path, *, frequency, leading=0, trailing=0):
        sample_rate = 8_000
        tone = [
            int(6_000 * math.sin(2 * math.pi * frequency * index / sample_rate))
            for index in range(4_000)
        ]
        samples = [0] * leading + tone + [0] * trailing
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    def make_report(self, root, *, scope="complete_exact_bank"):
        root = Path(root)
        candidates = []
        for index, media_id in enumerate((20, 10), start=1):
            reference = root / "references" / f"{media_id}.wav"
            self.write_wav(
                reference,
                frequency=200 + index * 50,
                leading=800 if media_id == 10 else 0,
                trailing=800 if media_id == 20 else 0,
            )
            candidates.append(
                {
                    "character": "Hotelier",
                    "portrait": "505401.png",
                    "source_bank": "hotelier.bnk",
                    "source_bank_sha256": "a" * 64,
                    "media_id": media_id,
                    "source_sha256": hashlib.sha256(
                        f"encoded-{media_id}".encode()
                    ).hexdigest(),
                    "candidate_origin": "exact_bank_unrouted_media",
                    "source_event_ids": [1_000 + media_id],
                    "reference": f"references/{media_id}.wav",
                    "reference_sha256": hashlib.sha256(
                        reference.read_bytes()
                    ).hexdigest(),
                    "source_lines": [],
                }
            )
        report = root / "report.json"
        report.write_text(
            json.dumps(
                {
                    "schema": "r1999.story-voice-reference-candidates",
                    "schema_version": 2,
                    "bank_inventory_scope": scope,
                    "groups": [
                        {
                            "character": "Hotelier",
                            "portrait": "505401.png",
                            "source_bank": "hotelier.bnk",
                            "candidate_count": 2,
                            "affected_portrait_line_count": 1,
                        }
                    ],
                    "candidates": candidates,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return report

    def test_publishes_all_exact_clips_and_checksum_ledger(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = self.make_report(root)

            result = publish_exact_bank_reference_composite(
                report,
                "Hotelier",
                "505401.png",
                "hotelier.bnk",
                root / "composite",
            )
            ledger = json.loads(
                (result.directory / "composite.json").read_text(encoding="utf-8")
            )
            evaluation = json.loads(
                (result.directory / "evaluation.json").read_text(encoding="utf-8")
            )
            queue = VoiceGenerationQueue.load(result.directory / "queue.jsonl")
            _manifest, voices = load_voice_manifest(
                result.directory / "voice-manifest.json", allow_legacy=False
            )

            self.assertEqual(ledger["schema"], COMPOSITE_SCHEMA)
            self.assertEqual(result.clips, 2)
            self.assertEqual([clip["media_id"] for clip in ledger["clips"]], [10, 20])
            self.assertTrue(ledger["clips"][0]["trimmed_leading_frames"] > 0)
            self.assertTrue(ledger["clips"][1]["trimmed_trailing_frames"] > 0)
            self.assertEqual(
                hashlib.sha256(
                    (result.directory / "composite.wav").read_bytes()
                ).hexdigest(),
                result.sha256,
            )
            self.assertEqual(
                ledger["composite"]["objective_preflight"]["objective_preflight"],
                "pass",
            )
            self.assertEqual(
                ledger["composite"]["objective_preflight"]["path"],
                "composite.wav",
            )
            self.assertEqual(len(queue.items), 3)
            self.assertEqual(len(evaluation["fixed_queue_ids"]), 3)
            self.assertEqual(voices[0].references, ("composite.wav",))
            self.assertEqual(queue.items[0].voice_character, voices[0].character)

    def test_rejects_story_routed_only_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = self.make_report(root, scope="story_routed_only")

            with self.assertRaisesRegex(ReferenceCompositeError, "complete exact-bank"):
                publish_exact_bank_reference_composite(
                    report,
                    "Hotelier",
                    "505401.png",
                    "hotelier.bnk",
                    root / "composite",
                )

    def test_publishes_composite_quality_card_without_binding_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = self.make_report(root)
            composite = publish_exact_bank_reference_composite(
                report,
                "Hotelier",
                "505401.png",
                "hotelier.bnk",
                root / "composite",
            )
            generation = run_bulk_generation(
                composite.directory / "queue.jsonl",
                root / "generation",
                _Renderer(),
                provider="synthetic",
                model="synthetic-v1",
                generation_profile="stable",
            )

            quality = publish_composite_quality_review(
                composite.directory, generation.state, root / "quality"
            )
            session = load_source_reference_quality_review(quality.session)

            self.assertEqual(quality.generated_samples, 3)
            card = session["variants"][0]
            self.assertEqual(card["reference_kind"], "exact_bank_composite")
            self.assertEqual(card["media_ids"], [10, 20])
            self.assertEqual(len(card["generated_samples"]), 3)
            self.assertIn("not a source-reference plan", session["authority"])

    def test_rejects_changed_reference_and_existing_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = self.make_report(root)
            (root / "references" / "10.wav").write_bytes(b"changed")

            with self.assertRaisesRegex(ReferenceCompositeError, "checksum"):
                publish_exact_bank_reference_composite(
                    report,
                    "Hotelier",
                    "505401.png",
                    "hotelier.bnk",
                    root / "composite",
                )
            output = root / "exists"
            output.mkdir()
            with self.assertRaisesRegex(ReferenceCompositeError, "output exists"):
                publish_exact_bank_reference_composite(
                    report,
                    "Hotelier",
                    "505401.png",
                    "hotelier.bnk",
                    output,
                )


if __name__ == "__main__":
    unittest.main()

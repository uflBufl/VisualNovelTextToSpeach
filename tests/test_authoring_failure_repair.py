import unittest

import numpy as np

from vntts.authoring.failure_repair import (
    BOUNDED_SEED_RETRY,
    EDGE_SILENCE_TRIM,
    OFFLINE_FALLBACK_BACKEND,
    SENTENCE_BOUNDARY_SEGMENTATION,
    FailureRepairPolicy,
    FailureRepairPolicyError,
    render_sentence_segments,
    safe_sentence_segments,
    trim_excess_edge_silence,
)
from vntts.synthesis import (
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisRequest,
    SynthesisResult,
    SynthesisTiming,
)


class AuthoringFailureRepairTest(unittest.TestCase):
    def test_policy_is_canonical_exact_and_round_trips(self):
        policy = FailureRepairPolicy(
            ("line:b", "line:a"), ("line:c",), 200, ("line:d",), ("line:e",)
        )

        self.assertEqual(
            policy.queue_ids, ("line:a", "line:b", "line:c", "line:d", "line:e")
        )
        self.assertEqual(policy.strategy_for("line:a"), SENTENCE_BOUNDARY_SEGMENTATION)
        self.assertEqual(policy.strategy_for("line:c"), EDGE_SILENCE_TRIM)
        self.assertEqual(policy.strategy_for("line:d"), BOUNDED_SEED_RETRY)
        self.assertEqual(policy.strategy_for("line:e"), OFFLINE_FALLBACK_BACKEND)
        self.assertEqual(
            FailureRepairPolicy.from_document(policy.to_document()), policy
        )
        with self.assertRaisesRegex(FailureRepairPolicyError, "two"):
            FailureRepairPolicy(("line:a",), (), 180, ("line:a",))
        with self.assertRaisesRegex(FailureRepairPolicyError, "requires"):
            FailureRepairPolicy(segment_pause_ms=200)
        malformed = policy.to_document()
        malformed["sentence_segment_queue_ids"] = "line:a"
        with self.assertRaisesRegex(FailureRepairPolicyError, "JSON lists"):
            FailureRepairPolicy.from_document(malformed)
        legacy = policy.to_document()
        legacy["schema_version"] = 1
        legacy.pop("bounded_seed_retry_queue_ids")
        legacy.pop("offline_fallback_queue_ids")
        self.assertEqual(
            FailureRepairPolicy.from_document(legacy).bounded_seed_retry_queue_ids,
            (),
        )

    def test_sentence_segments_use_distinct_seeds_and_bounded_pause(self):
        requests = []

        def render(request):
            requests.append(request)
            pcm = np.full(100, len(requests), dtype=np.float32)

            def produce():
                yield SynthesisChunk(pcm, 1_000, 0, 1.0)
                return SynthesisResult(
                    pcm,
                    1_000,
                    SynthesisCompletion.COMPLETE,
                    SynthesisLimits(10, 1.0),
                    SynthesisTiming(1.0, 2.0),
                    SynthesisDiagnostics(
                        "synthetic",
                        "fresh-generation",
                        request.generation_profile,
                        request.seed,
                        1,
                        len(pcm),
                    ),
                )

            return SynthesisChunkStream(produce())

        request = SynthesisRequest("Hero", "Combined", seed=7)
        result = render_sentence_segments(
            render,
            request,
            ("First complete sentence.", "Second complete sentence."),
            pause_ms=180,
        )

        self.assertEqual(
            [value.text for value in requests],
            [
                "First complete sentence.",
                "Second complete sentence.",
            ],
        )
        self.assertEqual([value.seed for value in requests], [7, 8])
        self.assertEqual(len(result.pcm), 380)
        self.assertTrue(np.all(result.pcm[100:280] == 0))
        self.assertEqual(result.diagnostics.seed, 7)
        self.assertEqual(result.diagnostics.sample_count, 380)

    def test_sentence_segments_reject_inner_seed_drift_and_total_limit(self):
        def rendered(request, *, sample_count=100, seed=None):
            pcm = np.ones(sample_count, dtype=np.float32) * 0.2

            def produce():
                yield SynthesisChunk(pcm, 1_000, 0, 1.0)
                return SynthesisResult(
                    pcm,
                    1_000,
                    SynthesisCompletion.COMPLETE,
                    SynthesisLimits(10, 20.0),
                    SynthesisTiming(1.0, 2.0),
                    SynthesisDiagnostics(
                        "synthetic",
                        "fresh-generation",
                        request.generation_profile,
                        request.seed if seed is None else seed,
                        1,
                        len(pcm),
                    ),
                )

            return SynthesisChunkStream(produce())

        request = SynthesisRequest("Hero", "Combined", seed=7)
        with self.assertRaisesRegex(ValueError, "diagnostics"):
            render_sentence_segments(
                lambda value: rendered(value, seed=999),
                request,
                ("First complete sentence.", "Second complete sentence."),
                pause_ms=180,
            )
        limited = render_sentence_segments(
            lambda value: rendered(value, sample_count=11_000),
            request,
            ("First complete sentence.", "Second complete sentence."),
            pause_ms=0,
        )
        self.assertIs(limited.completion, SynthesisCompletion.LIMITED)
        self.assertEqual(limited.limits.max_audio_seconds, 20.0)

    def test_sentence_split_requires_complete_substantial_boundaries(self):
        self.assertEqual(
            safe_sentence_segments(
                "The gate is already open. We should leave before dawn."
            ),
            ("The gate is already open.", "We should leave before dawn."),
        )
        self.assertEqual(
            safe_sentence_segments("Mrs. Owen waits beside the gate."),
            ("Mrs. Owen waits beside the gate.",),
        )
        self.assertEqual(
            safe_sentence_segments("Wait here, and listen carefully."),
            ("Wait here, and listen carefully.",),
        )
        self.assertEqual(
            safe_sentence_segments("Stop! Go."),
            ("Stop! Go.",),
        )

    def test_edge_trim_removes_only_excess_boundary_silence(self):
        sample_rate = 1_000
        speech = np.full(500, 0.2, dtype=np.float32)
        internal = np.zeros(300, dtype=np.float32)
        samples = np.concatenate(
            (
                np.zeros(1_000, dtype=np.float32),
                speech,
                internal,
                speech,
                np.zeros(900, dtype=np.float32),
            )
        )

        result = trim_excess_edge_silence(samples, sample_rate)

        self.assertEqual(result.leading_trimmed_samples, 920)
        self.assertEqual(result.trailing_trimmed_samples, 820)
        self.assertEqual(len(result.pcm), len(samples) - 1_740)
        self.assertTrue(np.array_equal(result.pcm[580:880], internal))
        self.assertTrue(np.all(result.pcm[:80] == 0))
        self.assertTrue(np.all(result.pcm[-80:] == 0))

    def test_short_edges_and_all_silence_are_not_changed(self):
        short = np.concatenate(
            (
                np.zeros(100, dtype=np.float32),
                np.ones(200, dtype=np.float32) * 0.2,
                np.zeros(100, dtype=np.float32),
            )
        )
        untouched = trim_excess_edge_silence(short, 1_000)
        silent = np.zeros(2_000, dtype=np.float32)
        silent_result = trim_excess_edge_silence(silent, 1_000)

        self.assertEqual(untouched.leading_trimmed_samples, 0)
        self.assertEqual(untouched.trailing_trimmed_samples, 0)
        self.assertTrue(np.array_equal(untouched.pcm, short))
        self.assertTrue(np.array_equal(silent_result.pcm, silent))


if __name__ == "__main__":
    unittest.main()

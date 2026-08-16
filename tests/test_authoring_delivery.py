import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts import VoiceGenerationQueue, write_story_index_document
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.voice_manifest import write_voice_manifest

from vntts.authoring.cli import main as authoring_main
from vntts.authoring.delivery import (
    DELIVERY_ANNOTATION_VERSION,
    LEGACY_ENGLISH_POLICY,
    PRESERVE_DELIVERY_POLICY,
    DeliveryAnnotationError,
    annotate_delivery,
    apply_delivery_policy,
)
from vntts.authoring.queue_builder import (
    inspect_generation_queue,
    publish_generation_queue,
)


def story_record(line_id, text=None, **extensions):
    text = text or f"Exact text for {line_id}."
    return {
        "record_type": "line",
        "line_id": line_id,
        "chapter": "chapter-one",
        "sequence": int(line_id.rsplit("-", 1)[-1]),
        "speaker": "Ada",
        "voice_character": "Ada",
        "text": text,
        "text_sha256": text_sha256(text),
        "kind": "dialogue",
        "previous_text": "Previous happy thought.",
        "next_text": "Next line.",
        "source_audio_status": "absent",
        "source_kind": "story",
        "speakable": True,
        **extensions,
    }


def write_inputs(root, records):
    story = root / "story.jsonl"
    write_story_index_document(
        story,
        {
            "game": "Synthetic Novel",
            "language": "en",
            "generated_at": "2026-08-17T08:00:00+00:00",
        },
        records,
    )
    reference = root / "references" / "ada.wav"
    write_pcm16_wav(reference, [0.0, 0.1, -0.1, 0.0], 16_000)
    voices = root / "voices.json"
    write_voice_manifest(
        voices,
        {
            "version": 2,
            "voices": [
                {
                    "character": "Ada",
                    "speaker": "provider-ada",
                    "references": ["references/ada.wav"],
                }
            ],
        },
    )
    return story, voices


class DeliveryAnnotationTest(unittest.TestCase):
    def test_exact_legacy_fear_annotation_and_model_prompts(self):
        annotation = annotate_delivery(
            "Help! Run from the monster!", speaker="Test Hero"
        )

        self.assertEqual(annotation["annotation_version"], 1)
        self.assertEqual(
            annotation["emotion"],
            {"primary": "fear", "confidence": 0.9, "cues": ["exclamation"]},
        )
        self.assertEqual(
            annotation["delivery"],
            {
                "pace": "fast",
                "energy": "high",
                "volume": "loud",
                "tone": "uneasy and urgent",
            },
        )
        prompt = (
            "Perform as Test Hero. Emotion: fear. Tone: uneasy and urgent. "
            "Pace: fast. Energy: high. Volume: loud."
        )
        self.assertEqual(annotation["prompt_adapters"]["generic"], prompt)
        self.assertEqual(
            annotation["prompt_adapters"]["chatterbox"],
            {"prompt": prompt, "exaggeration": 0.7, "cfg_weight": 0.45},
        )

    def test_narration_only_changes_zero_score_default(self):
        narration = annotate_delivery("The synthetic room is quiet.", kind="narration")
        dialogue = annotate_delivery(
            "The synthetic room is quiet.", kind="sound_effect"
        )
        spaced_kind = annotate_delivery(
            "The synthetic room is quiet.", kind=" narration "
        )

        self.assertEqual(narration["emotion"]["primary"], "contemplation")
        self.assertEqual(narration["delivery"]["pace"], "slow")
        self.assertEqual(dialogue["emotion"]["primary"], "neutral")
        self.assertEqual(spaced_kind["emotion"]["primary"], "neutral")

    def test_legacy_tie_and_cue_order_are_deterministic(self):
        tied = annotate_delivery("happy sad")
        cued = annotate_delivery(
            "HELP MONSTER!!! ...??",
            previous_text="happy then stop",
        )

        self.assertEqual(tied["emotion"]["primary"], "sadness")
        self.assertEqual(
            cued["emotion"]["cues"],
            [
                "exclamation",
                "ellipsis",
                "repeated_question",
                "uppercase_emphasis",
                "context:joy",
            ],
        )

    def test_policy_generation_is_non_mutating_and_has_separate_provenance(self):
        source = {
            "text": "Help!",
            "speaker": "Ada",
            "kind": "dialogue",
            "producer_extension": {"keep": True},
        }
        before = json.loads(json.dumps(source))

        application = apply_delivery_policy(source, LEGACY_ENGLISH_POLICY)

        self.assertEqual(source, before)
        self.assertEqual(application.origin, "policy")
        self.assertEqual(application.record["producer_extension"], {"keep": True})
        self.assertEqual(application.provenance["origin"], "policy")
        self.assertEqual(application.provenance["policy"], LEGACY_ENGLISH_POLICY)
        self.assertRegex(application.provenance["input_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("vntts.authoring.delivery", application.record)

    def test_source_annotations_are_preserved_and_partial_is_not_completed(self):
        complete = {
            "text": "Source-owned.",
            "speaker": "Ada",
            "kind": "dialogue",
            "annotation_version": DELIVERY_ANNOTATION_VERSION,
            "emotion": {"primary": "source"},
            "delivery": {"pace": "source"},
            "prompt_adapters": {"generic": "source"},
            "vntts.authoring.delivery": {"source": True},
        }
        partial = {
            "text": "Partial.",
            "speaker": "Ada",
            "kind": "dialogue",
            "emotion": None,
        }

        complete_result = apply_delivery_policy(complete, LEGACY_ENGLISH_POLICY)
        partial_result = apply_delivery_policy(partial, LEGACY_ENGLISH_POLICY)

        self.assertEqual(complete_result.origin, "source_complete")
        self.assertEqual(complete_result.record, complete)
        self.assertEqual(partial_result.origin, "source_partial")
        self.assertEqual(partial_result.record, partial)
        self.assertNotIn("delivery", partial_result.record)
        self.assertIsNone(complete_result.provenance)

    def test_missing_or_wrong_source_version_is_partial(self):
        base = {
            "text": "Source-owned.",
            "speaker": "Ada",
            "kind": "dialogue",
            "emotion": {},
            "delivery": {},
            "prompt_adapters": {},
        }
        self.assertEqual(apply_delivery_policy(base).origin, "source_partial")
        self.assertEqual(
            apply_delivery_policy({**base, "annotation_version": True}).origin,
            "source_partial",
        )
        self.assertEqual(
            apply_delivery_policy({**base, "annotation_version": 2}).origin,
            "source_partial",
        )

    def test_invalid_policy_and_inputs_are_actionable(self):
        with self.assertRaisesRegex(DeliveryAnnotationError, "Unsupported"):
            apply_delivery_policy({}, "unknown")
        with self.assertRaisesRegex(DeliveryAnnotationError, "text"):
            annotate_delivery("")


class DeliveryQueueIntegrationTest(unittest.TestCase):
    def test_default_and_explicit_preserve_plans_are_identical_and_lossless(self):
        record = story_record(
            "line-1",
            emotion={"primary": "source"},
            prompt_adapters={"generic": "source"},
            **{"vntts.authoring.delivery": {"source": True}},
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, voices = write_inputs(root, [record])
            implicit = inspect_generation_queue(
                story,
                voices,
                generated_at="2026-08-17T08:05:00+00:00",
            )
            explicit = inspect_generation_queue(
                story,
                voices,
                delivery_policy=PRESERVE_DELIVERY_POLICY,
                generated_at="2026-08-17T08:05:00+00:00",
            )
            implicit_path = publish_generation_queue(implicit, root / "implicit.jsonl")
            explicit_path = publish_generation_queue(explicit, root / "explicit.jsonl")
            implicit_bytes = implicit_path.read_bytes()
            explicit_bytes = explicit_path.read_bytes()

        self.assertEqual(implicit.metadata, explicit.metadata)
        self.assertEqual(implicit.items, explicit.items)
        self.assertEqual(implicit_bytes, explicit_bytes)
        self.assertEqual(
            implicit.items[0]["vntts.authoring.delivery"], {"source": True}
        )
        self.assertNotIn("delivery_annotation_policy", implicit.metadata)

    def test_opt_in_overlay_marks_only_policy_generated_items_in_metadata(self):
        generated = story_record("line-1", "Help! Run from the monster!")
        partial = story_record("line-2", emotion={"primary": "source"})
        complete = story_record(
            "line-3",
            annotation_version=1,
            emotion={"primary": "source"},
            delivery={"pace": "source"},
            prompt_adapters={"generic": "source"},
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, voices = write_inputs(root, [generated, partial, complete])
            preserved = inspect_generation_queue(
                story,
                voices,
                generated_at="2026-08-17T08:05:00+00:00",
            )
            annotated = inspect_generation_queue(
                story,
                voices,
                delivery_policy=LEGACY_ENGLISH_POLICY,
                generated_at="2026-08-17T08:05:00+00:00",
            )

        self.assertEqual(
            [item["queue_id"] for item in annotated.items],
            [item["queue_id"] for item in preserved.items],
        )
        self.assertEqual(
            [item["text_sha256"] for item in annotated.items],
            [item["text_sha256"] for item in preserved.items],
        )
        self.assertEqual(annotated.items[0]["emotion"]["primary"], "fear")
        self.assertNotIn("delivery", annotated.items[1])
        self.assertEqual(annotated.items[2]["delivery"], {"pace": "source"})
        policy = annotated.metadata["delivery_annotation_policy"]
        self.assertEqual(
            {
                key: policy[key]
                for key in (
                    "name",
                    "version",
                    "mode",
                    "policy_generated_count",
                    "source_complete_count",
                    "source_partial_count",
                    "unannotated_count",
                )
            },
            {
                "name": LEGACY_ENGLISH_POLICY,
                "version": 1,
                "mode": "missing-only",
                "policy_generated_count": 1,
                "source_complete_count": 1,
                "source_partial_count": 1,
                "unannotated_count": 0,
            },
        )
        self.assertEqual(
            policy["generated_items"][0]["queue_id"],
            annotated.items[0]["queue_id"],
        )

    def test_cli_annotation_and_queue_overlay_are_explicit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, voices = write_inputs(
                root, [story_record("line-1", "Help! Run from the monster!")]
            )
            queue_path = root / "queue.jsonl"
            queue_stdout = io.StringIO()
            with redirect_stdout(queue_stdout):
                self.assertEqual(
                    authoring_main(
                        [
                            "build-queue",
                            "--story-index",
                            str(story),
                            "--voice-manifest",
                            str(voices),
                            "--delivery-policy",
                            LEGACY_ENGLISH_POLICY,
                            "--output",
                            str(queue_path),
                        ]
                    ),
                    0,
                )
            annotation_stdout = io.StringIO()
            with redirect_stdout(annotation_stdout):
                self.assertEqual(
                    authoring_main(
                        [
                            "annotate-delivery",
                            "--text",
                            "Help! Run from the monster!",
                            "--speaker",
                            "Ada",
                        ]
                    ),
                    0,
                )

            queue = VoiceGenerationQueue.load(queue_path)
            annotation = json.loads(annotation_stdout.getvalue())

        self.assertEqual(queue.items[0].document["emotion"]["primary"], "fear")
        self.assertEqual(
            queue.metadata["delivery_annotation_policy"]["name"],
            LEGACY_ENGLISH_POLICY,
        )
        self.assertEqual(annotation["annotation"]["emotion"]["primary"], "fear")
        self.assertEqual(annotation["provenance"]["policy"], LEGACY_ENGLISH_POLICY)


if __name__ == "__main__":
    unittest.main()

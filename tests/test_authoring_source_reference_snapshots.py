import hashlib
import json
import unittest
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.voice_manifest import write_voice_manifest

from tests.source_reference_fixtures import publish_source_reference_quality_fixture
from vntts.authoring import source_reference_quality_records as quality_records
from vntts.authoring import source_reference_review as source_review


class SourceReferenceSnapshotsTest(unittest.TestCase):
    def quality_inputs(self, root, **arguments):
        root.mkdir(parents=True, exist_ok=True)
        plan, evaluation, _generation, quality = (
            publish_source_reference_quality_fixture(root, **arguments)
        )
        session = quality_records.load_source_reference_quality_review(quality.session)
        for card in session["variants"]:
            quality_records.record_source_reference_quality_decision(
                quality.session, card["variant_id"], "accept"
            )
        return plan, evaluation, quality

    def base_manifest(self, root):
        path = root / "base-manifest.json"
        write_voice_manifest(
            path,
            {
                "version": 2,
                "game": "Synthetic",
                "language": "en",
                "voices": [
                    {
                        "character": "Narrator",
                        "speaker": "narrator",
                        "references": ["references/1.wav"],
                    }
                ],
            },
        )
        return path

    @contextmanager
    def transient_loader_document(self, module, name, path, replacement):
        original = path.read_bytes()
        loader = getattr(module, name)
        loader_path = path.parent if name == "load_source_reference_plan" else path

        def load_replacement(*arguments, **keywords):
            if Path(arguments[0]).resolve() != loader_path.resolve():
                return loader(*arguments, **keywords)
            path.write_text(json.dumps(replacement), encoding="utf-8")
            try:
                return loader(*arguments, **keywords)
            finally:
                path.write_bytes(original)

        with patch.object(module, name, side_effect=load_replacement):
            yield original
        self.assertEqual(path.read_bytes(), original)

    def assert_narrator_snapshot(self, manifest_path):
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        narrator = next(
            voice for voice in document["voices"] if voice["character"] == "Narrator"
        )
        self.assertEqual(narrator["speaker"], "narrator")
        return document[source_review.SOURCE_REFERENCE_BINDINGS_FIELD]

    def test_listening_reports_refuse_model_from_another_state_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _plan, evaluation, generation, _quality = (
                publish_source_reference_quality_fixture(root)
            )
            original = generation.state.read_bytes()
            replacement = json.loads(original)
            for item in replacement["items"].values():
                item["model"] = "different-admissible-model"
            replacement_payload = json.dumps(replacement).encode("utf-8")
            read_bytes, read_text = Path.read_bytes, Path.read_text
            captures = []

            def read_replacement(reader, path, *arguments, **keywords):
                if path.resolve() != generation.state.resolve():
                    return reader(path, *arguments, **keywords)
                path.write_bytes(replacement_payload)
                try:
                    captures.append(path)
                    return reader(path, *arguments, **keywords)
                finally:
                    path.write_bytes(original)

            output = root / "listening-reports"
            with (
                patch.object(
                    Path,
                    "read_bytes",
                    autospec=True,
                    side_effect=partial(read_replacement, read_bytes),
                ),
                patch.object(
                    Path,
                    "read_text",
                    autospec=True,
                    side_effect=partial(read_replacement, read_text),
                ),
                self.assertRaisesRegex(
                    source_review.SourceReferenceReviewError,
                    "generation state changed",
                ),
            ):
                source_review.publish_source_reference_listening_reports(
                    evaluation.directory, generation.state, output
                )
            self.assertTrue(captures)
            self.assertEqual(generation.state.read_bytes(), original)
            self.assertFalse(output.exists())
            self.assertFalse(list(root.glob(".listening-reports.staging-*")))

    def test_quality_selection_uses_the_review_bytes_in_its_provenance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, _evaluation, quality = self.quality_inputs(root)
            replacement = json.loads(quality.session.read_bytes())
            replacement["variants"][0]["decision"]["decision"] = "reject"
            with self.transient_loader_document(
                quality_records,
                "load_source_reference_quality_review",
                quality.session,
                replacement,
            ) as original:
                result = source_review.publish_source_reference_bindings(
                    plan.directory,
                    self.base_manifest(root),
                    "Narrator",
                    None,
                    root / "bindings",
                    quality_review=quality.session,
                )
            binding = self.assert_narrator_snapshot(
                result.directory / "voice-manifest.json"
            )
            self.assertEqual(result.selected_variants, len(replacement["variants"]))
            self.assertEqual(
                binding["source_reference_quality_review_sha256"],
                hashlib.sha256(original).hexdigest(),
            )

    def test_initial_binding_uses_the_captured_base_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, _evaluation, quality = self.quality_inputs(root)
            manifest = self.base_manifest(root)
            replacement = json.loads(manifest.read_bytes())
            replacement["voices"][0]["speaker"] = "transient-narrator"
            with self.transient_loader_document(
                source_review, "load_voice_manifest", manifest, replacement
            ):
                result = source_review.publish_source_reference_bindings(
                    plan.directory,
                    manifest,
                    "Narrator",
                    None,
                    root / "bindings",
                    quality_review=quality.session,
                )
            self.assert_narrator_snapshot(result.directory / "voice-manifest.json")

    def test_successor_and_retirement_use_their_predecessor_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, _evaluation, first_quality = self.quality_inputs(root / "first")
            second, _evaluation, second_quality = self.quality_inputs(
                root / "second", character="Guide", line_prefix="guide-"
            )
            base = source_review.publish_source_reference_bindings(
                first.directory,
                self.base_manifest(root / "first"),
                "Narrator",
                None,
                root / "base",
                quality_review=first_quality.session,
            )
            base_path = base.directory / "voice-manifest.json"
            publish_successor = partial(
                source_review.publish_source_reference_binding_successor,
                base_path,
                second.directory,
                second_quality.session,
                "Narrator",
            )
            multi = publish_successor(root / "multi")
            multi_path = multi.directory / "voice-manifest.json"
            multi_document = json.loads(multi_path.read_bytes())
            retired_id = multi_document[source_review.SOURCE_REFERENCE_BINDINGS_FIELD][
                "selected_variants"
            ][-1]["variant_id"]
            for name, predecessor, publish in (
                ("successor", base_path, publish_successor),
                (
                    "retirement",
                    multi_path,
                    partial(
                        source_review.publish_source_reference_binding_retirement,
                        multi_path,
                        (retired_id,),
                    ),
                ),
            ):
                with self.subTest(publisher=name):
                    replacement = json.loads(predecessor.read_bytes())
                    replacement["voices"][0]["speaker"] = "transient-narrator"
                    with self.transient_loader_document(
                        source_review,
                        "load_voice_manifest",
                        predecessor,
                        replacement,
                    ) as original:
                        result = publish(root / name)
                    binding = self.assert_narrator_snapshot(
                        result.directory / "voice-manifest.json"
                    )
                    self.assertEqual(
                        binding["predecessor_manifest_sha256"],
                        hashlib.sha256(original).hexdigest(),
                    )

    def test_binding_and_evaluation_use_the_plan_bytes_in_their_provenance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, evaluation, _quality = self.quality_inputs(root)
            plan_path = plan.directory / "plan.json"
            original_document = source_review.load_source_reference_plan(plan.directory)
            variants = [
                f"{cluster['cluster_id']}-anchor-1"
                for cluster in original_document["clusters"]
            ]
            queue_items = sum(
                len(cluster["queue_items"]) for cluster in original_document["clusters"]
            )
            replacement = json.loads(plan_path.read_bytes())
            for cluster in replacement["clusters"]:
                cluster["queue_items"] = []
                for reference in cluster["references"]:
                    reference["source_transcripts"] = []
            manifest = self.base_manifest(root)
            for name, publish, count_field, expected_count, document_name in (
                (
                    "bindings",
                    partial(
                        source_review.publish_source_reference_bindings,
                        plan.directory,
                        manifest,
                        "Narrator",
                        variants,
                    ),
                    "bound_queue_items",
                    queue_items,
                    "voice-manifest.json",
                ),
                (
                    "evaluation",
                    partial(
                        source_review.publish_source_reference_evaluation,
                        plan.directory,
                    ),
                    "queue_items",
                    evaluation.queue_items,
                    "comparison.json",
                ),
            ):
                with self.subTest(publisher=name):
                    with self.transient_loader_document(
                        source_review,
                        "load_source_reference_plan",
                        plan_path,
                        replacement,
                    ) as original:
                        result = publish(root / f"snapshot-{name}")
                    self.assertEqual(getattr(result, count_field), expected_count)
                    document = json.loads(
                        (result.directory / document_name).read_bytes()
                    )
                    if name == "bindings":
                        document = document[
                            source_review.SOURCE_REFERENCE_BINDINGS_FIELD
                        ]
                    self.assertEqual(
                        document["source_reference_plan_sha256"],
                        hashlib.sha256(original).hexdigest(),
                    )


if __name__ == "__main__":
    unittest.main()

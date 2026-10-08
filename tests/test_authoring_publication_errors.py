import json
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr
from functools import partial
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.voice_manifest import write_voice_manifest

from tests.source_reference_fixtures import publish_source_reference_quality_fixture
from vntts.authoring import source_reference_review
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.publication import AtomicPublicationError, publication_errors
from vntts.authoring.source_reference_quality import (
    load_source_reference_quality_review,
    record_source_reference_quality_decision,
)
from vntts.authoring.source_reference_review import SourceReferenceReviewError


def publication_calls(root: Path) -> dict[str, Callable[[Path], object]]:
    first = root / "first"
    second = root / "second"
    first.mkdir()
    second.mkdir()
    first_plan, evaluation, generation, first_quality = (
        publish_source_reference_quality_fixture(first)
    )
    second_plan, _evaluation, _generation, second_quality = (
        publish_source_reference_quality_fixture(
            second, character="Guide", line_prefix="guide-"
        )
    )
    for quality in (first_quality, second_quality):
        review = load_source_reference_quality_review(quality.session)
        for variant in review["variants"]:
            record_source_reference_quality_decision(
                quality.session, variant["variant_id"], "accept"
            )
    manifest = root / "voice-manifest.json"
    write_voice_manifest(
        manifest,
        {
            "version": 2,
            "voices": [
                {
                    "character": "Narrator",
                    "speaker": "narrator",
                    "references": ["first/references/1.wav"],
                }
            ],
        },
    )
    bindings = source_reference_review.publish_source_reference_bindings(
        first_plan.directory,
        manifest,
        "Narrator",
        None,
        root / "bindings",
        quality_review=first_quality.session,
    )
    successor = source_reference_review.publish_source_reference_binding_successor(
        bindings.directory / "voice-manifest.json",
        second_plan.directory,
        second_quality.session,
        "Narrator",
        root / "successor",
    )
    successor_manifest = successor.directory / "voice-manifest.json"
    document = json.loads(successor_manifest.read_text(encoding="utf-8"))
    variant_id = document["vntts.authoring.source_reference_bindings"][
        "selected_variants"
    ][0]["variant_id"]
    return {
        "plan": partial(
            source_reference_review.import_source_reference_review,
            first / "report.json",
            first / "review.json",
            first / "story.jsonl",
        ),
        "bindings": partial(
            source_reference_review.publish_source_reference_bindings,
            first_plan.directory,
            manifest,
            "Narrator",
            None,
            quality_review=first_quality.session,
        ),
        "successor": partial(
            source_reference_review.publish_source_reference_binding_successor,
            bindings.directory / "voice-manifest.json",
            second_plan.directory,
            second_quality.session,
            "Narrator",
        ),
        "retirement": partial(
            source_reference_review.publish_source_reference_binding_retirement,
            successor_manifest,
            (variant_id,),
        ),
        "evaluation": partial(
            source_reference_review.publish_source_reference_evaluation,
            first_plan.directory,
        ),
        "listening": partial(
            source_reference_review.publish_source_reference_listening_reports,
            evaluation.directory,
            generation.state,
        ),
    }


class PublicationErrorsTest(unittest.TestCase):
    def test_source_reference_publishers_preserve_sources_and_cleanup_on_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            sources = root / "sources"
            sources.mkdir()
            calls = publication_calls(sources)
            source_tree = {
                path.relative_to(sources): path.read_bytes() if path.is_file() else None
                for path in sources.rglob("*")
            }
            rename = source_reference_review.rename_directory_no_replace
            for name, publish in calls.items():
                for stage in ("staging", "write", "rename", "collision"):
                    with self.subTest(publisher=name, stage=stage):
                        output = root / f"{name}-{stage}"
                        failure = PermissionError("Publication was blocked")

                        def publish_or_fail(staging: Path, destination: Path) -> None:
                            if destination == output:
                                if stage == "rename":
                                    raise failure
                                output.mkdir()
                                (output / "sentinel").write_bytes(b"existing output")
                            rename(staging, destination)

                        if stage == "staging":
                            target = "staged_directory"
                        elif stage == "write":
                            target = {
                                "plan": "atomic_write_json",
                                "listening": "atomic_write_json",
                                "evaluation": "write_voice_generation_queue",
                            }.get(name, "write_voice_manifest")
                        else:
                            target = "rename_directory_no_replace"
                        side_effect = (
                            failure
                            if stage in {"staging", "write"}
                            else publish_or_fail
                        )
                        with (
                            patch.object(
                                source_reference_review, target, side_effect=side_effect
                            ),
                            self.assertRaises(SourceReferenceReviewError) as caught,
                        ):
                            publish(output)
                        if stage == "collision":
                            self.assertIsInstance(
                                caught.exception.__cause__, AtomicPublicationError
                            )
                            self.assertEqual(
                                {
                                    path.name: path.read_bytes()
                                    for path in output.iterdir()
                                },
                                {"sentinel": b"existing output"},
                            )
                            (output / "sentinel").unlink()
                            output.rmdir()
                        else:
                            self.assertIs(caught.exception.__cause__, failure)
                            self.assertFalse(output.exists())
                        self.assertEqual(set(root.iterdir()), {sources})
                        self.assertEqual(
                            {
                                path.relative_to(sources): (
                                    path.read_bytes() if path.is_file() else None
                                )
                                for path in sources.rglob("*")
                            },
                            source_tree,
                        )

            stderr = StringIO()
            failure = PermissionError("Publication was blocked")
            with (
                patch.object(
                    source_reference_review, "staged_directory", side_effect=failure
                ),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as caught,
            ):
                authoring_main(
                    [
                        "build-reference-evaluation",
                        "--plan",
                        str(sources / "first" / "plan"),
                        "--output",
                        str(root / "cli-output"),
                    ]
                )
            self.assertEqual(caught.exception.code, 2)
            self.assertIn(str(failure), stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertEqual(set(root.iterdir()), {sources})

    def test_existing_domain_errors_are_not_wrapped(self):
        failure = SourceReferenceReviewError("Existing validation error")
        with self.assertRaises(SourceReferenceReviewError) as caught:
            with publication_errors(SourceReferenceReviewError):
                raise failure
        self.assertIs(caught.exception, failure)
        self.assertIsNone(caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()

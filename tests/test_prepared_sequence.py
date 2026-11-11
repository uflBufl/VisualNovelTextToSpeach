import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from r1999extractor.live_sequence import (
    build_live_sequence_document,
)
from r1999extractor.live_sequence import (
    write_live_sequence_plan as write_reverse1999_live_sequence_plan,
)
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.live_sequence import write_live_sequence_plan
from vntts_artifacts.story_index import write_story_index

import vntts.prepared_sequence as prepared_sequence
from vntts.prepared_sequence import (
    PreparedSequenceCancelled,
    PreparedSequenceError,
    prepare_reverse1999_sequence,
    prepare_sequence_for_prepared_story,
    reverse1999_live_sequence_command,
)


def write_story(path, *, source_bundle=None):
    write_story_index(
        path,
        {
            "game": "Reverse: 1999",
            "language": "en",
            **({"source_bundle": str(source_bundle)} if source_bundle else {}),
        },
        [
            {
                "record_type": "line",
                "line_id": "reverse1999:314501:1",
                "chapter": "314501",
                "sequence": 1,
                "speaker": "A",
                "text": "First.",
                "kind": "dialogue",
                "text_sha256": hashlib.sha256(b"First.").hexdigest(),
            },
            {
                "record_type": "line",
                "line_id": "reverse1999:314501:2",
                "chapter": "314501",
                "sequence": 2,
                "speaker": "B",
                "text": "Second.",
                "kind": "dialogue",
                "text_sha256": hashlib.sha256(b"Second.").hexdigest(),
            },
        ],
    )


def write_plan(path, story, bundle, *, mode="safe"):
    events = [
        {
            "event_id": "reverse1999:314501:event:1",
            "sequence": 1,
            "kind": "speech",
            "line_id": "reverse1999:314501:1",
            "control": "automatic",
            "successors": ["reverse1999:314501:event:2"],
        },
        {
            "event_id": "reverse1999:314501:event:2",
            "sequence": 2,
            "kind": "speech",
            "line_id": "reverse1999:314501:2",
            "control": "terminal",
            "successors": [],
        },
    ]
    if mode == "branch":
        events[0]["control"] = "manual"
        events[0]["successors"] = [
            "reverse1999:314501:event:2",
            "reverse1999:314501:event:3",
        ]
        events.append(
            {
                "event_id": "reverse1999:314501:event:3",
                "sequence": 3,
                "kind": "transition",
                "control": "terminal",
                "successors": [],
            }
        )
    elif mode == "gap":
        events[1] = {
            "event_id": "reverse1999:314501:event:2",
            "sequence": 2,
            "kind": "wait",
            "control": "manual",
            "successors": [],
        }
    document = {
        "schema": "vntts.live-sequence-plan",
        "schema_version": 1,
        "game_id": "reverse1999",
        "producer": {"name": "test", "version": "1"},
        "story_index_sha256": sha256_file(story),
        "source_extract_sha256": sha256_file(bundle),
        "chapters": [
            {
                "chapter": "314501",
                "entry_event_ids": ["reverse1999:314501:event:1"],
                "events": events,
            }
        ],
    }
    return write_live_sequence_plan(path, document, story)


class FinishedPublisher:
    def __init__(self, arguments, publish):
        self.arguments = arguments
        self.returncode = 0
        self._publish = publish

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        del timeout
        self._publish(self.arguments)
        return "published", ""


class CancellingPublisher:
    def __init__(self, cancellation):
        self.cancellation = cancellation
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        if self.terminated:
            return "", ""
        self.cancellation.set()
        raise subprocess.TimeoutExpired("publisher", timeout)

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        return self.returncode


class PreparedSequenceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.story = self.root / "story-index.jsonl"
        self.bundle = self.root / "story.dat"
        self.output = self.root / "live-sequence.json"
        self.bundle.write_bytes(b"source bundle")
        write_story(self.story)

    def _write_export_story(self):
        documents = {}
        records = []
        for chapter, texts in (
            ("315601", ("First chapter.",)),
            ("315602", ("Before.", "...?!", "...!", "…？！ — 「」 ♪", "After.")),
        ):
            steps = []
            for sequence, text in enumerate(texts, 1):
                payload = [None] * 18
                payload[11] = ["", "", "Ada"]
                payload[15] = ["", "", text]
                steps.append(
                    [sequence, f"step-{sequence}", payload, *[[] for _ in range(8)]]
                )
                if sequence in (1, len(texts)):
                    records.append(
                        {
                            "record_type": "line",
                            "line_id": f"reverse1999:{chapter}:{sequence}",
                            "chapter": chapter,
                            "sequence": sequence,
                            "speaker": "Ada",
                            "text": text,
                            "kind": "dialogue",
                        }
                    )
            documents[f"json_story_step_{chapter}"] = ["title", "", steps]
            documents[f"json_story_group_{chapter}"] = [
                [[sequence, sequence + 1, ""]] for sequence in range(1, len(texts))
            ]
        write_story_index(
            self.story,
            {
                "game": "Reverse: 1999",
                "language": "en",
                "source_bundle": str(self.bundle),
            },
            records,
        )
        return documents

    def _prepare_exported_story(self, documents, output):
        def publish(arguments):
            document = build_live_sequence_document(
                documents, self.story, self.bundle, producer_version="test"
            )
            write_reverse1999_live_sequence_plan(
                document,
                Path(arguments[arguments.index("--output") + 1]),
                self.story,
            )

        return prepare_sequence_for_prepared_story(
            self.story,
            output,
            command=("publisher",),
            popen_factory=lambda arguments, **_kwargs: FinishedPublisher(
                arguments, publish
            ),
        )

    def test_exported_combined_chapters_prepare_with_mixed_silent_punctuation(self):
        documents = self._write_export_story()

        plan = self._prepare_exported_story(documents, self.output)

        self.assertEqual(
            tuple(chapter.chapter for chapter in plan.chapters), ("315601", "315602")
        )
        self.assertEqual(plan.source_extract_sha256, sha256_file(self.bundle))
        self.assertEqual(
            tuple(plan.event_id_by_line_id),
            (
                "reverse1999:315601:1",
                "reverse1999:315602:1",
                "reverse1999:315602:5",
            ),
        )
        for sequence in (2, 3, 4):
            with self.subTest(sequence=sequence):
                event = plan.events[f"reverse1999:315602:event:{sequence}"]
                self.assertEqual(event.kind, "silent")
                self.assertEqual(event.control, "automatic")
                self.assertIsNone(event.line_id)
                self.assertEqual(
                    event.successors,
                    (f"reverse1999:315602:event:{sequence + 1}",),
                )

    def test_exported_choice_or_unindexed_words_still_reject_combined_chapters(self):
        for boundary in ("choice", "unindexed-speech"):
            with self.subTest(boundary=boundary):
                documents = self._write_export_story()
                step = documents["json_story_step_315602"][2][1]
                if boundary == "choice":
                    step[10] = [
                        [
                            False,
                            0,
                            "",
                            ["", "", ""],
                            ["", "", "Continue"],
                            0,
                            0,
                            "0",
                            False,
                            0,
                            3,
                        ]
                    ]
                else:
                    step[2][15][2] = "Wait...?!"
                output = self.root / f"{boundary}.json"

                with self.assertRaisesRegex(PreparedSequenceError, "manual boundary"):
                    self._prepare_exported_story(documents, output)

                self.assertFalse(output.exists())

    def test_reuses_exact_checksum_bound_plan_without_starting_publisher(self):
        write_plan(self.output, self.story, self.bundle)
        publisher = Mock()

        plan = prepare_reverse1999_sequence(
            self.story,
            self.bundle,
            ("314501",),
            self.output,
            command=("publisher",),
            popen_factory=publisher,
        )

        self.assertEqual(plan.path, self.output.resolve())
        publisher.assert_not_called()

    def test_prepared_story_uses_its_exact_bundle_and_chapters(self):
        write_story(self.story, source_bundle=self.bundle)
        started = []

        def publish(arguments):
            started.append(arguments)
            write_plan(
                Path(arguments[arguments.index("--output") + 1]),
                self.story,
                self.bundle,
            )

        plan = prepare_sequence_for_prepared_story(
            self.story,
            self.output,
            command=("publisher",),
            popen_factory=lambda arguments, **_kwargs: FinishedPublisher(
                arguments, publish
            ),
        )

        self.assertEqual(plan.path, self.output.resolve())
        self.assertEqual(started[0].count("--chapter"), 1)

    def test_replaces_stale_plan_with_selected_exact_inputs(self):
        stale_bundle = self.root / "old-story.dat"
        stale_bundle.write_bytes(b"old")
        write_plan(self.output, self.story, stale_bundle)
        started = []

        def publish(arguments):
            started.append(arguments)
            write_plan(
                Path(arguments[arguments.index("--output") + 1]),
                self.story,
                self.bundle,
            )

        plan = prepare_reverse1999_sequence(
            self.story,
            self.bundle,
            ("314501",),
            self.output,
            command=("publisher",),
            popen_factory=lambda arguments, **_kwargs: FinishedPublisher(
                arguments, publish
            ),
        )

        self.assertEqual(plan.source_extract_sha256, sha256_file(self.bundle))
        self.assertEqual(started[0][:1], ("publisher",))
        self.assertEqual(
            started[0][started[0].index("--story-index") + 1], str(self.story.resolve())
        )
        self.assertEqual(
            started[0][started[0].index("--bundle") + 1], str(self.bundle.resolve())
        )
        self.assertEqual(started[0].count("--chapter"), 1)

    def test_rejects_manual_branch_or_gap_without_replacing_output(self):
        for mode in ("branch", "gap"):
            with self.subTest(mode=mode):
                output = self.root / f"{mode}.json"

                def publish(arguments, mode=mode):
                    write_plan(
                        Path(arguments[arguments.index("--output") + 1]),
                        self.story,
                        self.bundle,
                        mode=mode,
                    )

                with self.assertRaisesRegex(PreparedSequenceError, "manual boundary"):
                    prepare_reverse1999_sequence(
                        self.story,
                        self.bundle,
                        ("314501",),
                        output,
                        command=("publisher",),
                        popen_factory=lambda arguments, **_kwargs: FinishedPublisher(
                            arguments, publish
                        ),
                    )
                self.assertFalse(output.exists())

    def test_rejects_malformed_publisher_output_without_replacing_output(self):
        def publish(arguments):
            Path(arguments[arguments.index("--output") + 1]).write_text(
                "{}", encoding="utf-8"
            )

        with self.assertRaisesRegex(
            PreparedSequenceError, "Invalid live-sequence plan"
        ):
            prepare_reverse1999_sequence(
                self.story,
                self.bundle,
                ("314501",),
                self.output,
                command=("publisher",),
                popen_factory=lambda arguments, **_kwargs: FinishedPublisher(
                    arguments, publish
                ),
            )
        self.assertFalse(self.output.exists())

    def test_malformed_output_preserves_primary_when_staging_cleanup_fails(self):
        real_temporary_directory = tempfile.TemporaryDirectory
        cleanup_error = OSError("staging cleanup failed")

        def temporary_directory(*args, **kwargs):
            directory = real_temporary_directory(*args, **kwargs)
            if kwargs.get("prefix") == f".{self.output.name}-":
                cleanup = directory.cleanup

                def failing_cleanup():
                    cleanup()
                    raise cleanup_error

                directory.cleanup = failing_cleanup
            return directory

        def publish(arguments):
            Path(arguments[arguments.index("--output") + 1]).write_text(
                "{}", encoding="utf-8"
            )

        with (
            patch("vntts.cleanup.TemporaryDirectory", side_effect=temporary_directory),
            self.assertRaisesRegex(
                PreparedSequenceError, "Invalid live-sequence plan"
            ) as caught,
        ):
            prepare_reverse1999_sequence(
                self.story,
                self.bundle,
                ("314501",),
                self.output,
                command=("publisher",),
                popen_factory=lambda arguments, **_kwargs: FinishedPublisher(
                    arguments, publish
                ),
            )

        self.assertIn("staging cleanup failed", " ".join(caught.exception.__notes__))
        self.assertFalse(self.output.exists())

    def test_cancellation_terminates_publisher_without_publishing_output(self):
        cancellation = Event()
        process = CancellingPublisher(cancellation)

        with self.assertRaises(PreparedSequenceCancelled):
            prepare_reverse1999_sequence(
                self.story,
                self.bundle,
                ("314501",),
                self.output,
                command=("publisher",),
                cancellation=cancellation,
                popen_factory=lambda *_arguments, **_kwargs: process,
            )

        self.assertTrue(process.terminated)
        self.assertFalse(self.output.exists())

    def test_frozen_build_uses_planned_worker_flag(self):
        with (
            patch.object(prepared_sequence.shutil, "which", return_value=None),
            patch.object(
                prepared_sequence.importlib.util, "find_spec", return_value=object()
            ),
            patch.object(sys, "frozen", True, create=True),
        ):
            self.assertEqual(
                reverse1999_live_sequence_command(),
                (sys.executable, "--prepared-sequence-worker"),
            )


if __name__ == "__main__":
    unittest.main()

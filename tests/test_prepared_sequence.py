import hashlib
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

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

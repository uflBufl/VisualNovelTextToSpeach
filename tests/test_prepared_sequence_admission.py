"""Prepared sequence parsing owns admitted snapshots, not changing source paths."""

import hashlib
import io
import os
import stat
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import vntts_artifacts.live_sequence as artifacts

import tests.test_prepared_sequence as fixtures
from tests.regular_file_fixtures import swap_at_acquisition
from vntts.prepared_sequence import (
    PreparedSequenceError,
    prepare_reverse1999_sequence,
    prepare_sequence_for_prepared_story,
)


class PreparedSequenceAdmissionTest(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.PreparedSequenceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.story, self.bundle, self.output = (
            path.resolve()
            for path in (fixture.root, fixture.story, fixture.bundle, fixture.output)
        )

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "native FIFO"
    )
    def test_swapped_inputs_refuse_acquisition_close_and_retry(self):
        fixtures.write_story(self.story, source_bundle=self.bundle)
        fixtures.write_plan(self.output, self.story, self.bundle)
        for phase in ("metadata-story", "cached-story", "cached-plan"):
            with self.subTest(phase=phase):
                gate = {
                    "path": self.output if phase == "cached-plan" else self.story,
                    "armed": True,
                }
                with swap_at_acquisition(self, gate):
                    with self.assertRaisesRegex(
                        PreparedSequenceError,
                        "Unable to read prepared story"
                        if phase == "metadata-story"
                        else "publisher is not installed",
                    ):
                        if phase == "metadata-story":
                            prepare_sequence_for_prepared_story(
                                self.story, self.output, command=()
                            )
                        else:
                            prepare_reverse1999_sequence(
                                self.story,
                                self.bundle,
                                ("314501",),
                                self.output,
                                command=(),
                            )
                publisher = Mock()
                plan = prepare_sequence_for_prepared_story(
                    self.story,
                    self.output,
                    command=("publisher",),
                    popen_factory=publisher,
                )
                publisher.assert_not_called()
                self.assertEqual(plan.path, self.output.resolve())

    def test_plan_parser_uses_captured_story_bytes_and_original_paths(self):
        fixtures.write_plan(self.output, self.story, self.bundle)
        original = self.story.read_bytes()
        changed = original.replace(b"First.", b"Altered.").replace(
            hashlib.sha256(b"First.").hexdigest().encode(),
            hashlib.sha256(b"Altered.").hexdigest().encode(),
        )
        real_load = artifacts._load_story_lines
        parsed_texts, parsed_paths = [], []

        def load_after_mutation(path):
            self.story.write_bytes(changed)
            lines = real_load(path)
            parsed_texts.append(lines["reverse1999:314501:1"].text)
            parsed_paths.append(Path(path))
            return lines

        try:
            with patch.object(
                artifacts, "_load_story_lines", side_effect=load_after_mutation
            ):
                plan = prepare_reverse1999_sequence(
                    self.story, self.bundle, ("314501",), self.output, command=()
                )
            self.assertEqual(parsed_texts, ["First."])
            self.assertEqual(plan.path, self.output.resolve())
            self.assertEqual(plan.story_index_path, self.story.resolve())
            self.assertEqual(
                plan.story_index_sha256, hashlib.sha256(original).hexdigest()
            )
            self.assertNotEqual(parsed_paths, [self.story])
            self.assertTrue(all(not path.exists() for path in parsed_paths))
        finally:
            self.story.write_bytes(original)

    def test_pack_reader_uses_captured_story_bytes_and_admits_its_source(self):
        import vntts.pregeneration_pack as pack

        fixtures.write_plan(self.output, self.story, self.bundle)
        original = self.story.read_bytes()
        changed = original.replace(b"First.", b"Altered.").replace(
            hashlib.sha256(b"First.").hexdigest().encode(),
            hashlib.sha256(b"Altered.").hexdigest().encode(),
        )
        parsed = []
        real_load = artifacts._load_story_lines

        def load_after_mutation(path):
            self.story.write_bytes(changed)
            lines = real_load(path)
            parsed.append(lines["reverse1999:314501:1"].text)
            return lines

        try:
            with patch.object(
                artifacts, "_load_story_lines", side_effect=load_after_mutation
            ):
                document, plan = pack._safe_sequence_document(self.output, self.story)
            self.assertEqual(parsed, ["First."])
            self.assertEqual(plan.path, self.output)
            self.assertEqual(plan.story_index_path, self.story)
            self.assertEqual(
                plan.story_index_sha256, hashlib.sha256(original).hexdigest()
            )
            self.assertEqual(document["chapters"][0]["chapter"], "314501")
        finally:
            self.story.write_bytes(original)
        if hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"):
            with swap_at_acquisition(self, {"path": self.story, "armed": True}):
                with self.assertRaisesRegex(OSError, "not a regular file"):
                    pack._safe_sequence_document(self.output, self.story)
            _document, recovered = pack._safe_sequence_document(self.output, self.story)
            self.assertEqual(
                recovered.story_index_sha256, hashlib.sha256(original).hexdigest()
            )

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "native FIFO"
    )
    def test_controller_default_factory_refuses_swaps_and_retries(self):
        from vntts.chapter_voice_preload import ChapterVoicePreloader
        from vntts.controller import AppController
        from vntts.settings import AppSettings

        fixtures.write_plan(self.output, self.story, self.bundle)
        preloader = ChapterVoicePreloader.load_optional(self.story)
        self.assertIsNotNone(preloader)
        settings = AppSettings(
            story_index=str(self.story),
            live_sequence_plan=str(self.output),
            live_sequence_mode="shadow",
        )
        for source in (self.output, self.story):
            with self.subTest(source=source.name):
                messages = []
                with swap_at_acquisition(self, {"path": source, "armed": True}):
                    controller = AppController(
                        settings,
                        tts_factory=Mock(),
                        chapter_voice_preloader=preloader,
                        status_handler=messages.append,
                    )
                    self.addCleanup(controller.shutdown)
                    self.assertEqual(
                        controller.get_live_sequence_status().state, "unavailable"
                    )
                    self.assertTrue(
                        any("not a regular file" in message for message in messages)
                    )
                recovered = AppController(
                    settings,
                    tts_factory=Mock(),
                    chapter_voice_preloader=preloader,
                    status_handler=lambda *_args: None,
                )
                self.addCleanup(recovered.shutdown)
                self.assertIsNotNone(recovered.story_cursor)
                self.assertEqual(recovered.live_sequence_plan.path, self.output)
                self.assertEqual(
                    recovered.live_sequence_plan.story_index_path, self.story
                )

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "native FIFO"
    )
    def test_nonregular_publisher_candidate_preserves_output_and_cleans_staging(self):
        stale = self.root / "old-bundle.dat"
        stale.write_bytes(b"old")
        fixtures.write_plan(self.output, self.story, stale)
        original = self.output.read_bytes()
        native_io_open = io.open
        candidates = []

        def guard(path, *args, **kwargs):
            if isinstance(path, (str, Path)) and Path(path).exists():
                self.assertFalse(
                    stat.S_ISFIFO(Path(path).stat().st_mode), "blocking candidate open"
                )
            return native_io_open(path, *args, **kwargs)

        def publish(arguments):
            candidate = Path(arguments[arguments.index("--output") + 1])
            candidates.append(candidate)
            os.mkfifo(candidate)

        with patch("io.open", side_effect=guard):
            with self.assertRaisesRegex(
                PreparedSequenceError, "Invalid live-sequence plan"
            ):
                prepare_reverse1999_sequence(
                    self.story,
                    self.bundle,
                    ("314501",),
                    self.output,
                    command=("publisher",),
                    popen_factory=lambda arguments, **_kwargs: (
                        fixtures.FinishedPublisher(arguments, publish)
                    ),
                )
        self.assertEqual(self.output.read_bytes(), original)
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0].parent.exists())

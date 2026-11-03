"""Playback consumes committed SQLite results without a mutable live JSON file."""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

from tests.bulk_generation_fixtures import SyntheticRenderer, queue_item, write_queue
from tests.symlink_support import symlink_or_skip
from vntts import generated_audio
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    run_bulk_generation,
    validate_generation_output,
)
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.generation_database import read_state, state_path, write_state


class GenerationPublicationContentionTest(unittest.TestCase):
    def test_state_validation_keeps_canonical_workspace_file_guards(self):
        for name in ("workspace.json", "queue.jsonl"):
            for alias in (False, True):
                with (
                    self.subTest(name=name, alias=alias),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    queue = write_queue(root / "queue.jsonl", [queue_item()])
                    result = run_bulk_generation(
                        queue,
                        root / "output",
                        SyntheticRenderer(),
                        provider="synthetic",
                        model="synthetic-v1",
                    )
                    (root / "workspace.json").write_text("{}", encoding="utf-8")
                    validate_generation_output(result.state)
                    control = root / name
                    saved = root / (name + ".saved")
                    control.rename(saved)
                    if alias:
                        symlink_or_skip(control, saved)
                    else:
                        control.mkdir()
                    with self.assertRaisesRegex(BulkGenerationError, "requires"):
                        validate_generation_output(result.state)

    def test_commit_during_initial_load_is_seen_without_another_commit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            result = run_bulk_generation(
                queue,
                root / "output",
                SyntheticRenderer(),
                provider="synthetic",
                model="synthetic-v1",
            )
            load = generated_audio.load_generated_audio_source

            def reject_after_snapshot(path):
                snapshot = load(path)
                state = read_state(result.state)
                state["items"][item["queue_id"]]["review_status"] = "rejected"
                write_state(result.state, state)
                return snapshot

            with patch.object(
                generated_audio,
                "load_generated_audio_source",
                side_effect=reject_after_snapshot,
            ):
                library = GeneratedAudioLibrary.load_optional(result.state)
            self.assertIsNotNone(library)
            self.assertFalse(library.has_entry(item["line_id"], item["text_sha256"]))

    def test_playback_reloads_committed_results_while_generation_continues(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            items = [queue_item("one"), queue_item("two")]
            queue = write_queue(root / "queue.jsonl", items)
            output = root / "output"
            second_started, finish_second = Event(), Event()
            results, failures = [], []

            def inspect_state(request):
                if request.text == items[1]["text"]:
                    second_started.set()
                    if not finish_second.wait(5):
                        raise TimeoutError("Test did not release second synthesis")

            renderer = SyntheticRenderer(inspect_state=inspect_state)

            def generate():
                try:
                    results.append(
                        run_bulk_generation(
                            queue,
                            output,
                            renderer,
                            provider="synthetic",
                            model="synthetic-v1",
                            approve_validated_audio=True,
                        )
                    )
                except BaseException as error:
                    failures.append(error)

            worker = Thread(target=generate)
            worker.start()
            try:
                self.assertTrue(second_started.wait(5))
                library = GeneratedAudioLibrary.load_optional(state_path(output))
                self.assertIsNotNone(library)
                self.assertTrue(library.runtime_progress)
                self.assertIsNotNone(
                    library.find(items[0]["line_id"], items[0]["text_sha256"])
                )
                self.assertIsNone(
                    library.find(items[1]["line_id"], items[1]["text_sha256"])
                )
                self.assertFalse((output / "manifest.json").exists())
                legacy = output / "live-progress-manifest.json"
                self.assertFalse(legacy.exists())
                legacy.write_bytes(b"stale legacy index")
                replace = os.replace

                def refuse_legacy_replace(source, destination):
                    if Path(destination) == legacy:
                        raise PermissionError("Reader holds the obsolete live index")
                    return replace(source, destination)

                with (
                    legacy.open("rb") as reader,
                    patch(
                        "durable_file.publication.os.replace",
                        side_effect=refuse_legacy_replace,
                    ),
                ):
                    finish_second.set()
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(reader.read(), b"stale legacy index")
                self.assertEqual(failures, [])
                self.assertEqual(results[0].generated, 2)
                self.assertEqual(results[0].failed, 0)
                self.assertIsNotNone(
                    library.find(items[1]["line_id"], items[1]["text_sha256"])
                )
                self.assertEqual(
                    {
                        entry["status"]
                        for entry in read_state(state_path(output))["items"].values()
                    },
                    {"approved"},
                )
                self.assertEqual(len(renderer.requests), 2)
                self.assertFalse((output / "manifest.json").exists())
            finally:
                finish_second.set()
                worker.join(5)

import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import write_story_index_document
from vntts_artifacts.voice_generation_queue import write_voice_generation_queue

from tests.generation_state_fixtures import replace_generation_state
from tests.pregeneration_fixtures import fixture
from tests.pregeneration_fixtures import item as pack_item
from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from vntts.authoring import generation_state
from vntts.authoring.generation_lease import BulkGenerationError
from vntts.generation_database import read_state
from vntts.pregeneration_generation import (
    OfflineGenerationError,
    OfflineGenerationWorker,
)
from vntts.pregeneration_pack import (
    OfflinePackError,
    OfflinePackPublisher,
    load_saved_pack,
)
from vntts.pregeneration_setup import (
    PregenerationJob,
    PregenerationSetupError,
    PreparationEstimate,
    estimate_generation_resources,
    estimate_preparation,
    inspect_story_index,
)
from vntts.pregeneration_voices import VoicePlan


class PregenerationResourcesTest(unittest.TestCase):
    def test_initial_estimate_uses_selected_text_not_machine_minutes(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "story-index.jsonl"
            text = "A short line with twenty seven characters."
            write_story_index_document(
                path,
                {"game": "Synthetic", "language": "en"},
                [
                    {
                        "record_type": "line",
                        "line_id": "one",
                        "chapter": "1",
                        "sequence": 1,
                        "speaker": "Narrator",
                        "text": text,
                        "kind": "dialogue",
                        "source_audio_status": "absent",
                        "speakable": True,
                    }
                ],
            )

            content = inspect_story_index(path)
            estimate = estimate_preparation(
                content, (content.selections[0].selection_id,)
            )

        self.assertEqual(estimate.generation_text_characters, len(text))
        self.assertGreater(estimate.rough_audio_seconds, 0)
        self.assertEqual(estimate.estimated_generation_minutes, 0)

    def test_resource_estimate_ignores_queue_replacement_after_capture(self):
        with TemporaryDirectory() as directory:
            _job, generation_input, result, items = fixture(Path(directory))
            result.state.unlink()
            original_read = generation_state.read_regular_bytes
            replaced = []

            def read_then_replace(path):
                payload = original_read(path)
                if path == generation_input.queue:
                    replaced.append(path)
                    write_voice_generation_queue(
                        path,
                        {"game": "Synthetic Game", "language": "en"},
                        [pack_item("changed", items[0]["sequence"]), items[1]],
                    )
                return payload

            with patch.object(
                generation_state, "read_regular_bytes", read_then_replace
            ):
                estimate = estimate_generation_resources(generation_input)

            self.assertEqual(replaced, [generation_input.queue])
            self.assertNotEqual(
                sha256_file(generation_input.queue), generation_input.queue_sha256
            )
            self.assertEqual(estimate.total_items, 2)
            self.assertEqual(
                estimate.total_text_characters, sum(len(item["text"]) for item in items)
            )

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_resource_estimate_queue_capture_refuses_fifo_and_recovers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _job, generation_input, _result, _items = fixture(root)
            queue = generation_input.queue
            queue.chmod(0o444)
            mode = stat.S_IMODE(queue.stat().st_mode)
            before_paths = set(root.rglob("*"))
            before = {
                path: path.read_bytes() for path in root.rglob("*") if path.is_file()
            }
            expected = estimate_generation_resources(generation_input)
            original_read = generation_state.read_regular_bytes
            refused = []

            def refuse_at_capture(path):
                self.assertEqual(path, queue)
                gate: RegularFileGate = {"path": queue, "armed": True}
                with (
                    swap_at_acquisition(self, gate),
                    self.assertRaisesRegex(OSError, "regular file") as caught,
                ):
                    original_read(path)
                refused.append(caught.exception)
                raise caught.exception

            with (
                patch.object(generation_state, "read_regular_bytes", refuse_at_capture),
                self.assertRaisesRegex(
                    PregenerationSetupError,
                    "Unable to estimate remaining offline audio storage",
                ) as caught,
            ):
                estimate_generation_resources(generation_input)

            self.assertEqual(len(refused), 1)
            self.assertIsInstance(caught.exception.__cause__, BulkGenerationError)
            self.assertIs(caught.exception.__cause__.__cause__, refused[0])
            self.assertEqual(str(caught.exception.__cause__), str(refused[0]))
            self.assertEqual(stat.S_IMODE(queue.stat().st_mode), mode)
            self.assertEqual(set(root.rglob("*")), before_paths)
            self.assertEqual(
                {path: path.read_bytes() for path in root.rglob("*") if path.is_file()},
                before,
            )
            self.assertEqual(estimate_generation_resources(generation_input), expected)
            self.assertEqual(
                {path: path.read_bytes() for path in root.rglob("*") if path.is_file()},
                before,
            )
            self.assertEqual(set(root.rglob("*")), before_paths)

    def test_resource_estimate_excludes_completed_audio_when_resuming(self):
        with TemporaryDirectory() as temporary_directory:
            _job, inputs, result, items = fixture(Path(temporary_directory))
            state = read_state(result.state)
            del state["items"][items[1]["queue_id"]]
            replace_generation_state(result.state, state)

            estimate = estimate_generation_resources(inputs)

        self.assertEqual((estimate.total_items, estimate.remaining_items), (2, 1))
        self.assertEqual(estimate.remaining_text_characters, len(items[1]["text"]))

    def test_saved_job_without_new_rough_fields_still_loads(self):
        job = PregenerationJob(
            job_id="a" * 24,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            status="planned",
            provider_id="test",
            game="Test",
            game_version=None,
            story_index="/test/story-index.jsonl",
            story_index_sha256="b" * 64,
            selected_story_ids=("one",),
            selected_line_ids=("line",),
            estimate=PreparationEstimate(1, 0, 1, 1, 1, 48_000),
        )
        document = job.to_document()
        document["estimate"].pop("generation_text_characters")
        document["estimate"].pop("rough_audio_seconds")

        loaded = PregenerationJob.from_document(document)

        self.assertEqual(loaded.estimate.generation_text_characters, 0)
        self.assertEqual(loaded.estimate.rough_audio_seconds, 0)

    def test_generation_stops_before_worker_when_remaining_audio_will_not_fit(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, inputs, result, items = fixture(root)
            state = read_state(result.state)
            del state["items"][items[1]["queue_id"]]
            replace_generation_state(result.state, state)
            before = {
                path: path.read_bytes() for path in root.rglob("*") if path.is_file()
            }
            plan = VoicePlan(
                job_id=job.job_id,
                created_at=job.created_at,
                story_index_sha256=inputs.story_index_sha256,
                voice_manifest=None,
                voice_manifest_sha256=None,
                synthesis_backend="pocket-tts",
                synthesis_model=None,
                synthesis_language="en",
                synthesis_profile="default",
                pocket_voice_cloning=False,
                synthesis_controls_sha256="d" * 64,
                groups=(),
            )
            popen = Mock(
                side_effect=AssertionError("worker started with no free space")
            )
            with (
                patch(
                    "vntts.pregeneration_generation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=0),
                ),
                self.assertRaisesRegex(
                    OfflineGenerationError,
                    "Free space or choose fewer stories.*saved work stays",
                ),
            ):
                OfflineGenerationWorker(
                    command=("worker",), popen_factory=popen
                ).generate(inputs, plan)
            popen.assert_not_called()
            self.assertEqual(
                {path: path.read_bytes() for path in root.rglob("*") if path.is_file()},
                before,
            )

    def test_pack_stops_before_staging_when_audio_copy_will_not_fit(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, inputs, result, _items = fixture(root)
            before_paths = set(root.rglob("*"))
            before = {
                path: path.read_bytes() for path in root.rglob("*") if path.is_file()
            }
            with (
                patch(
                    "vntts.pregeneration_pack.shutil.disk_usage",
                    return_value=SimpleNamespace(free=0),
                ),
                self.assertRaisesRegex(
                    OfflinePackError,
                    "Free space or choose fewer stories.*saved work stays",
                ),
            ):
                OfflinePackPublisher().publish(job, inputs, result)
            self.assertEqual(set(root.rglob("*")), before_paths)
            self.assertEqual(
                {path: path.read_bytes() for path in root.rglob("*") if path.is_file()},
                before,
            )

    def test_pack_wraps_disk_preflight_file_errors(self):
        with TemporaryDirectory() as temporary_directory:
            job, inputs, result, _items = fixture(Path(temporary_directory))
            with (
                patch(
                    "vntts.pregeneration_pack._pack_staging_bytes",
                    side_effect=FileNotFoundError("reference disappeared"),
                ),
                self.assertRaisesRegex(
                    OfflinePackError,
                    "Unable to inspect files needed for offline pack publication",
                ),
            ):
                OfflinePackPublisher().publish(job, inputs, result)

    def test_saved_pack_loader_rejects_any_damaged_generated_audio(self):
        from vntts_artifacts.generated_audio import load_generated_audio_document

        with TemporaryDirectory() as temporary_directory:
            job, inputs, result, _items = fixture(Path(temporary_directory))
            published = OfflinePackPublisher().publish(job, inputs, result)
            generated = load_generated_audio_document(
                published.imported.generated_audio_manifest
            )
            generated.records[0].audio.write_bytes(b"damaged")

            with self.assertRaisesRegex(OfflinePackError, "checksum does not match"):
                load_saved_pack(published.manifest)


if __name__ == "__main__":
    unittest.main()

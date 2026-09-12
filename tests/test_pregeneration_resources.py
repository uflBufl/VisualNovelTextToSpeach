import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import write_story_index_document

from vntts.pregeneration_generation import (
    OfflineGenerationError,
    OfflineGenerationWorker,
)
from vntts.pregeneration_pack import (
    OfflinePackError,
    OfflinePackPublisher,
    load_saved_pack,
)
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_setup import (
    GenerationResourceEstimate,
    PregenerationJob,
    PreparationEstimate,
    estimate_generation_resources,
    estimate_preparation,
    inspect_story_index,
)
from vntts.pregeneration_voices import VoicePlan


def _inputs(root):
    identity = "a" * 64
    directory = root / f"generation-input-{identity[:16]}"
    directory.mkdir(parents=True)
    queue = directory / "queue.jsonl"
    queue.write_text("queue", encoding="utf-8")
    return PregenerationInput(
        identity=identity,
        directory=directory,
        story_index=directory / "story-index.jsonl",
        voice_manifest=directory / "voice-manifest.json",
        queue=queue,
        queue_sha256=sha256_file(queue),
        queue_items=2,
        ready_items=2,
        narrator_fallback_roles=(),
    )


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
        self.assertEqual(estimate.rough_audio_seconds, (len(text) + 11) // 12)
        self.assertEqual(estimate.estimated_generation_minutes, 0)

    def test_resource_estimate_excludes_completed_audio_when_resuming(self):
        with TemporaryDirectory() as temporary_directory:
            inputs = _inputs(Path(temporary_directory))
            output = inputs.directory.parent / "generation-output-aaaaaaaaaaaaaaaa"
            output.mkdir()
            state = output / "generation-state.json"
            state.write_text("{}", encoding="utf-8")
            items = (
                SimpleNamespace(queue_id="done", action="generate", text="Done."),
                SimpleNamespace(
                    queue_id="retry", action="generate", text="Retry this."
                ),
            )
            with (
                patch(
                    "vntts.pregeneration_setup.VoiceGenerationQueue.load",
                    return_value=SimpleNamespace(items=items),
                ),
                patch(
                    "vntts.pregeneration_setup.load_generation_state",
                    return_value={"items": {"done": {"status": "approved"}}},
                ),
            ):
                estimate = estimate_generation_resources(inputs)

        self.assertEqual((estimate.total_items, estimate.remaining_items), (2, 1))
        self.assertEqual(estimate.remaining_text_characters, len("Retry this."))

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
            inputs = _inputs(Path(temporary_directory))
            plan = VoicePlan(
                job_id="b" * 24,
                created_at="2026-01-01T00:00:00+00:00",
                story_index_sha256="c" * 64,
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
            estimate = GenerationResourceEstimate(1, 1, 12, 12, 1, 1, 48_000, 48_000)
            with (
                patch(
                    "vntts.pregeneration_generation.estimate_generation_resources",
                    return_value=estimate,
                ),
                patch(
                    "vntts.pregeneration_generation.shutil.disk_usage",
                    return_value=SimpleNamespace(free=0),
                ),
                self.assertRaisesRegex(
                    OfflineGenerationError,
                    "Free space or choose fewer stories.*saved work stays",
                ),
            ):
                OfflineGenerationWorker(command=("worker",)).generate(inputs, plan)

    def test_pack_stops_before_staging_when_audio_copy_will_not_fit(self):
        from tests.test_pregeneration_pack import fixture

        with TemporaryDirectory() as temporary_directory:
            job, inputs, result, _items = fixture(Path(temporary_directory))
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

    def test_pack_wraps_disk_preflight_file_errors(self):
        from tests.test_pregeneration_pack import fixture

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

    def test_saved_pack_loader_rechecks_the_self_service_identity(self):
        from tests.test_pregeneration_pack import fixture

        with TemporaryDirectory() as temporary_directory:
            job, inputs, result, _items = fixture(Path(temporary_directory))
            published = OfflinePackPublisher().publish(job, inputs, result)

            loaded = load_saved_pack(published.manifest)

        self.assertEqual(loaded.identity, published.identity)

    def test_saved_pack_loader_rejects_any_damaged_generated_audio(self):
        from vntts_artifacts.generated_audio import load_generated_audio_document

        from tests.test_pregeneration_pack import fixture

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

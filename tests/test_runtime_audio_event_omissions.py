import copy
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue

from tests.bulk_generation_fixtures import SyntheticRenderer
from tests.pregeneration_fixtures import fixture
from vntts.authoring.bulk_generation import (
    _generation_progress_event,
    _reconcile_interrupted_attempt,
    run_bulk_generation,
)
from vntts.authoring.generation_lease import BulkGenerationError
from vntts.authoring.generation_state import (
    load_generation_state,
    write_generation_state,
)
from vntts.document_identity import sha256_file
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.pregeneration_generation import (
    OfflineGenerationWorker,
    _durable_ready_line_ids,
)
from vntts.pregeneration_pack import OfflinePackPublisher
from vntts.pregeneration_queue import (
    PregenerationQueueError,
    _self_service_omission_decisions,
    initialize_audio_event_omissions,
)


class RuntimeAudioEventOmissionsTest(unittest.TestCase):
    def test_generation_never_renders_or_regenerates_an_omitted_event(self):
        with TemporaryDirectory() as directory:
            _job, inputs, result, items = fixture(
                Path(directory), include_omission=True
            )
            renderer = SyntheticRenderer()
            options = dict(
                provider="synthetic",
                model="synthetic-v1",
                include_queue_ids=(items[-1]["queue_id"],),
            )
            resumed = run_bulk_generation(
                inputs.queue, result.output, renderer, **options
            )
            self.assertEqual(resumed.generated, 0)
            self.assertEqual(renderer.requests, [])
            with self.assertRaisesRegex(BulkGenerationError, "cannot overwrite"):
                run_bulk_generation(
                    inputs.queue,
                    result.output,
                    renderer,
                    regenerate_existing=True,
                    **options,
                )
            self.assertEqual(renderer.requests, [])

    def test_resume_preserves_omission_when_reconciling_an_interrupted_attempt(self):
        with TemporaryDirectory() as directory:
            _job, inputs, result, items = fixture(
                Path(directory), include_omission=True
            )
            original = load_generation_state(result.state, inputs.queue)
            queue_id = items[-1]["queue_id"]
            queue = VoiceGenerationQueue.load(inputs.queue)
            for phase, cancellation in (
                ("generating", None),
                ("retrying", {"requested": True}),
            ):
                with self.subTest(phase=phase):
                    state = copy.deepcopy(original)
                    state["active"] = {"queue_id": queue_id, "phase": phase}
                    write_generation_state(result.state, state)
                    initialize_audio_event_omissions(inputs, decided_at="resume")
                    state = load_generation_state(result.state, inputs.queue)
                    self.assertEqual(state["active"]["queue_id"], queue_id)
                    _reconcile_interrupted_attempt(
                        result.state, state, queue, cancellation=cancellation
                    )
                    resumed = load_generation_state(result.state, inputs.queue)
                    self.assertEqual(resumed["items"], original["items"])
                    self.assertIsNone(resumed["active"])

    def test_queue_change_during_initialization_does_not_publish(self):
        with TemporaryDirectory() as directory:
            _job, inputs, result, _items = fixture(
                Path(directory), include_omission=True
            )
            before = result.state.read_bytes()

            def change_queue(*args):
                records = _self_service_omission_decisions(*args)
                inputs.queue.write_bytes(inputs.queue.read_bytes() + b"\n")
                return records

            with (
                patch(
                    "vntts.pregeneration_queue._self_service_omission_decisions",
                    side_effect=change_queue,
                ),
                self.assertRaisesRegex(PregenerationQueueError, "queue changed"),
            ):
                initialize_audio_event_omissions(inputs, decided_at="resume")
            self.assertEqual(result.state.read_bytes(), before)

    def test_rejects_changed_outer_identity_and_foreign_omission_authority(self):
        with TemporaryDirectory() as directory:
            _job, inputs, result, items = fixture(
                Path(directory), include_omission=True
            )
            original = load_generation_state(result.state, inputs.queue)
            queue_id = items[-1]["queue_id"]
            for field in ("line_id", "text_sha256", "speaker"):
                with self.subTest(field=field):
                    state = copy.deepcopy(original)
                    state["items"][queue_id][field] = "f" * 64
                    write_generation_state(result.state, state)
                    with self.assertRaises(BulkGenerationError):
                        load_generation_state(result.state, inputs.queue)
            for field in ("queue_sha256", "base_workspace_sha256", "base_workspace_id"):
                with self.subTest(field=field):
                    state = copy.deepcopy(original)
                    state["items"][queue_id]["audio_event_omission"]["authority"][
                        field
                    ] = "f" * 64
                    write_generation_state(result.state, state)
                    before = result.state.read_bytes()
                    with self.assertRaisesRegex(
                        PregenerationQueueError, "authority changed"
                    ):
                        initialize_audio_event_omissions(inputs, decided_at="resume")
                    self.assertEqual(result.state.read_bytes(), before)

    def test_generation_fallback_resume_and_pack_keep_the_same_omission(self):
        with TemporaryDirectory() as directory:
            job, inputs, result, items = fixture(Path(directory), include_omission=True)
            state = load_generation_state(result.state, inputs.queue)
            omission_id = items[-1]["queue_id"]
            decision = state["items"][omission_id]["audio_event_omission"]
            wavs = {path: sha256_file(path) for path in result.output.rglob("*.wav")}
            self.assertEqual(state["items"][omission_id]["attempts"], 0)

            def published_decision(path):
                library = GeneratedAudioLibrary.load_optional(path)
                omission = library.find_audio_event_omission(
                    items[-1]["line_id"], items[-1]["text_sha256"]
                )
                self.assertIsNotNone(omission)
                self.assertIsNone(
                    library.find_audio_event_omission(items[-1]["line_id"], "f" * 64)
                )
                self.assertIsNone(
                    library.find(items[-1]["line_id"], items[-1]["text_sha256"])
                )
                return omission

            first = published_decision(result.state)
            initialize_audio_event_omissions(
                inputs, decided_at="2026-09-01T00:00:00+00:00"
            )
            resumed = load_generation_state(result.state, inputs.queue)
            self.assertEqual(
                resumed["items"][omission_id]["audio_event_omission"], decision
            )
            self.assertEqual(published_decision(result.state), first)
            pack = OfflinePackPublisher().publish(job, inputs, result)
            self.assertEqual(
                published_decision(pack.imported.generated_audio_manifest), first
            )
            self.assertEqual({path: sha256_file(path) for path in wavs}, wavs)

    def test_silent_line_is_ready_in_database_memory_and_cold_progress(self):
        with TemporaryDirectory() as directory:
            _job, inputs, result, items = fixture(
                Path(directory), include_omission=True
            )
            state = load_generation_state(result.state, inputs.queue)
            worker = OfflineGenerationWorker()
            database = worker.inspect_progress(inputs)
            self.assertIn(items[-1]["line_id"], database.ready_line_ids)
            self.assertIn(items[0]["line_id"], database.ready_line_ids)
            self.assertNotIn(items[1]["line_id"], database.ready_line_ids)
            self.assertEqual(database.generated, 1)
            self.assertEqual(database.other_terminal, 2)
            self.assertEqual(
                _durable_ready_line_ids(inputs, database), database.ready_line_ids
            )
            worker._record_progress(
                _generation_progress_event(state, state["items"], None), inputs
            )
            self.assertEqual(
                worker._live_progress[1].ready_line_ids, database.ready_line_ids
            )

    def test_omission_cannot_replace_spoken_text_or_an_unresolved_source(self):
        with TemporaryDirectory() as directory:
            _job, inputs, result, items = fixture(
                Path(directory),
                include_omission=True,
                omission_source_audio_status="unknown",
            )
            state = load_generation_state(result.state, inputs.queue)
            for queue_id in (items[0]["queue_id"], items[-1]["queue_id"]):
                with (
                    self.subTest(queue_id=queue_id),
                    self.assertRaisesRegex(
                        PregenerationQueueError, "omission is invalid"
                    ),
                ):
                    initialize_audio_event_omissions(
                        replace(inputs, audio_event_omission_queue_ids=(queue_id,)),
                        decided_at="2026-09-01T00:00:00+00:00",
                    )
                self.assertEqual(
                    load_generation_state(result.state, inputs.queue), state
                )

"""Explicit parent cancellation never refunds an unknown crash or completed result."""

import json
import socket
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import Mock, patch
from uuid import uuid4

from vntts_artifacts.file_integrity import sha256_file

import vntts.authoring.bulk_generation as bulk
from tests.bulk_generation_fixtures import SyntheticRenderer, queue_item, write_queue
from tests.test_pregeneration_generation import generation_inputs
from vntts.authoring.failure_repair import FailureRepairPolicy
from vntts.authoring.generation_lease import BulkGenerationError
from vntts.authoring.generation_state import load_generation_state
from vntts.generation_database import read_state, write_state
from vntts.pregeneration_generation import (
    OfflineGenerationCancelled,
    OfflineGenerationWorker,
)
from vntts.synthesis import SynthesisCompletion


class SubprocessGenerationCancellationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.item = queue_item()
        self.queue = write_queue(self.root / "queue.jsonl", [self.item])
        self.output = self.root / "output"
        self.state_path = self.output / "generation-state.sqlite3"
        self.lease_path = self.output / ".generation-lease.json"
        self.process = Mock(pid=999999, returncode=None)
        self.process.poll.side_effect = lambda: self.process.returncode

    def generate(self, renderer, **options):
        return bulk.run_bulk_generation(
            self.queue,
            self.output,
            renderer,
            provider="synthetic",
            model="synthetic-v1",
            generation_profile="stable",
            **options,
        )

    def interrupted(self, phase="generating", **options):
        original_phase = bulk._write_active_phase

        def persist_phase(*args, **kwargs):
            original_phase(*args, **kwargs)
            if args[2] == phase:
                raise SystemExit("child stopped")

        def stop_render(_request):
            if phase == "generating":
                raise SystemExit("child stopped")

        renderer = SyntheticRenderer(
            [SynthesisCompletion.LIMITED] if phase == "retrying" else None,
            inspect_state=stop_render,
        )
        with (
            patch.object(bulk, "_write_active_phase", side_effect=persist_phase),
            patch.object(
                bulk,
                "_store_successful_generation_attempt",
                side_effect=SystemExit("child stopped"),
            )
            if phase == "publishing"
            else patch.object(bulk, "_now", wraps=bulk._now),
        ):
            with self.assertRaises(SystemExit):
                self.generate(renderer, retries=1, **options)
        state = read_state(self.state_path)
        state["active"]["generation_owner_pid"] = self.process.pid
        write_state(self.state_path, state)
        self.write_lease()
        return state

    def write_lease(self):
        lease = {
            "schema": bulk.LEASE_SCHEMA,
            "schema_version": 1,
            "queue_sha256": sha256_file(self.queue),
            "pid": self.process.pid,
            "hostname": socket.gethostname(),
            "process_started_at": "owned-child-start",
            "lease_id": uuid4().hex,
            "started_at": "2026-10-23T00:00:00+00:00",
        }
        self.lease_path.write_text(json.dumps(lease))

    def witness(self):
        with patch.object(bulk, "process_started_at", return_value="owned-child-start"):
            witness = bulk.capture_generation_cancellation(
                self.output, self.queue, sha256_file(self.queue), self.process
            )
        self.assertIsNotNone(witness)
        return witness

    def finish_cancel(self, witness):
        self.process.returncode = -15
        return bulk.reconcile_generation_cancellation(witness, self.process)

    def test_incomplete_phases_refund_once_and_retain_unpublished_audio(self):
        for phase in ("generating", "validating", "publishing"):
            with self.subTest(phase=phase), TemporaryDirectory() as folder:
                self.root = Path(folder)
                self.queue = write_queue(self.root / "queue.jsonl", [self.item])
                self.output = self.root / "output"
                self.state_path = self.output / "generation-state.sqlite3"
                self.lease_path = self.output / ".generation-lease.json"
                self.process.returncode = None
                self.interrupted(phase)
                audio = self.output / bulk._audio_relative_path(
                    "Hero", self.item["queue_id"]
                )
                partial = audio.with_suffix(".partial.wav")
                if phase != "publishing":
                    partial.write_bytes(b"unpublished partial evidence")
                payload = audio.read_bytes() if audio.exists() else partial.read_bytes()
                witness = self.witness()
                self.assertTrue(self.finish_cancel(witness))
                state = load_generation_state(self.state_path, self.queue)
                record = state["items"][self.item["queue_id"]]
                self.assertIsNone(state["active"])
                self.assertEqual(record["failure"]["kind"], "cancelled")
                self.assertFalse(record["failure"]["attempt_counted"])
                self.assertEqual(record["attempts"], 0)
                self.assertEqual(record["attempts_by_provider"], {"synthetic": 0})
                self.assertFalse(audio.exists())
                self.assertFalse(partial.exists())
                self.assertIn(
                    payload,
                    [
                        path.read_bytes()
                        for path in (self.output / "interrupted").iterdir()
                    ],
                )
                self.assertEqual(
                    json.loads((self.output / "manifest.json").read_text())[
                        "entry_count"
                    ],
                    0,
                )
                audit = state["interrupted_attempts"][0]
                self.assertEqual(
                    audit["cancellation"]["lease_id"], witness.lease["lease_id"]
                )
                self.assertEqual(audit["generation_owner_pid"], self.process.pid)
                renderer = SyntheticRenderer()
                resumed = self.generate(renderer, retries=0)
                self.assertEqual(resumed.generated, 1)
                self.assertEqual([request.seed for request in renderer.requests], [0])

    def test_subprocess_cancel_preserves_last_bounded_attempt_for_resume(self):
        self.generate(
            SyntheticRenderer(
                [SynthesisCompletion.LIMITED, SynthesisCompletion.LIMITED]
            ),
            retries=1,
        )
        policy = FailureRepairPolicy(
            bounded_seed_retry_queue_ids=(self.item["queue_id"],)
        )
        self.interrupted(
            failure_repair_policy=policy, include_queue_ids=(self.item["queue_id"],)
        )
        witness = self.witness()
        self.finish_cancel(witness)
        state = load_generation_state(self.state_path, self.queue)
        record = state["items"][self.item["queue_id"]]
        self.assertEqual(record["attempts"], 2)
        self.assertEqual(record["attempts_by_provider"], {"synthetic": 2})
        self.assertEqual(
            record["failure"]["previous_failure"]["failure"]["kind"],
            "missed_eos_audio_limit",
        )
        renderer = SyntheticRenderer()
        result = self.generate(
            renderer,
            retries=2,
            failure_repair_policy=policy,
            include_queue_ids=(self.item["queue_id"],),
        )
        self.assertEqual(result.generated, 1)
        self.assertEqual([request.seed for request in renderer.requests], [2])

    def test_retrying_failure_is_already_completed_and_cannot_be_refunded(self):
        before = self.interrupted("retrying")["items"][self.item["queue_id"]]
        witness = self.witness()
        self.finish_cancel(witness)
        state = load_generation_state(self.state_path, self.queue)
        self.assertIsNone(state["active"])
        self.assertEqual(state["items"][self.item["queue_id"]], before)
        self.assertEqual(before["attempts"], 1)
        self.assertEqual(before["failure"]["kind"], "missed_eos_audio_limit")
        self.assertTrue(state["interrupted_attempts"][0]["attempt_counted"])

    def test_committed_success_race_preserves_approved_wav_and_publishes_it(self):
        result = self.generate(
            SyntheticRenderer(), retries=0, approve_validated_audio=True
        )
        before = load_generation_state(self.state_path, self.queue)
        record = before["items"][self.item["queue_id"]]
        audio = self.output / record["path"]
        payload = audio.read_bytes()
        self.write_lease()
        witness = self.witness()
        (self.output / "manifest.json").write_text("{}")
        self.finish_cancel(witness)
        self.assertEqual(load_generation_state(self.state_path, self.queue), before)
        self.assertEqual(audio.read_bytes(), payload)
        self.assertEqual(json.loads(result.manifest.read_text())["entry_count"], 1)

    def test_new_lease_owner_unconfirmed_exit_and_queue_change_do_not_rewrite_state(
        self,
    ):
        self.interrupted()
        witness = self.witness()
        original = self.state_path.read_bytes()
        with self.assertRaisesRegex(BulkGenerationError, "confirmed exit"):
            bulk.reconcile_generation_cancellation(witness, self.process)
        self.assertEqual(self.state_path.read_bytes(), original)
        lease = json.loads(self.lease_path.read_text())
        lease["lease_id"] = uuid4().hex
        self.lease_path.write_text(json.dumps(lease))
        self.process.returncode = -15
        with self.assertRaisesRegex(BulkGenerationError, "ownership changed"):
            bulk.reconcile_generation_cancellation(witness, self.process)
        self.assertEqual(json.loads(self.lease_path.read_text()), lease)
        self.assertEqual(self.state_path.read_bytes(), original)
        self.lease_path.write_text(json.dumps(witness.lease))
        self.queue.write_bytes(self.queue.read_bytes().rstrip(b"\n") + b" \n")
        with self.assertRaisesRegex(BulkGenerationError, "queue changed"):
            bulk.reconcile_generation_cancellation(witness, self.process)
        self.assertEqual(self.state_path.read_bytes(), original)

    @unittest.skipIf(sys.platform == "win32", "POSIX signal exit evidence")
    def test_coincident_crash_after_cancel_witness_is_not_relabelled(self):
        self.interrupted()
        witness = self.witness()
        before = self.state_path.read_bytes()
        self.process.returncode = -6
        self.assertFalse(bulk.reconcile_generation_cancellation(witness, self.process))
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_unknown_crash_keeps_existing_interrupted_accounting(self):
        self.interrupted()
        state = read_state(self.state_path)
        queue, _digest = bulk.load_stable_generation_queue(self.queue)
        bulk._reconcile_interrupted_attempt(self.state_path, state, queue)
        record = load_generation_state(self.state_path, self.queue)["items"][
            self.item["queue_id"]
        ]
        self.assertEqual(record["failure"]["kind"], "interrupted")
        self.assertEqual(record["attempts"], 1)

    def test_normal_subprocess_cancel_persists_typed_cancellation_after_real_child_exit(
        self,
    ):
        inputs, _plan = generation_inputs(self.root / "inputs")
        write_queue(inputs.queue, [self.item])
        inputs = replace(inputs, queue_sha256=sha256_file(inputs.queue))
        marker = self.root / "child-ready"
        script = """
import sys, time
from pathlib import Path
from tests.bulk_generation_fixtures import SyntheticRenderer
from vntts.authoring.bulk_generation import run_bulk_generation
def wait(request):
    Path(sys.argv[3]).write_text('ready')
    time.sleep(60)
run_bulk_generation(sys.argv[1], sys.argv[2], SyntheticRenderer(inspect_state=wait),
    provider='synthetic', model='synthetic-v1', generation_profile='stable', retries=0)
"""
        cancel = Event()
        finished = Event()

        def request_cancel():
            deadline = time.monotonic() + 5
            while (
                not marker.exists()
                and time.monotonic() < deadline
                and not finished.wait(0.01)
            ):
                pass
            cancel.set()

        trigger = Thread(target=request_cancel)
        trigger.start()
        started = time.monotonic()
        worker = OfflineGenerationWorker()
        try:
            with self.assertRaises(OfflineGenerationCancelled):
                worker._run_subprocess(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(inputs.queue),
                        str(inputs.output_directory),
                        str(marker),
                    ],
                    cancel,
                    generation_input=inputs,
                )
        finally:
            finished.set()
            trigger.join(2)
        self.assertTrue(marker.exists())
        self.assertLess(time.monotonic() - started, 8)
        state = load_generation_state(
            inputs.output_directory / "generation-state.sqlite3", inputs.queue
        )
        record = state["items"][self.item["queue_id"]]
        self.assertIsNone(state["active"])
        self.assertEqual(record["failure"]["kind"], "cancelled")
        self.assertEqual(record["attempts"], 0)
        self.assertIsNone(worker._process)

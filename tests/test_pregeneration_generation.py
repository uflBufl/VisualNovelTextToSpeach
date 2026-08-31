import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from vntts.pregeneration_generation import (
    OfflineGenerationCancelled,
    OfflineGenerationError,
    OfflineGenerationWorker,
)
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_voices import VoicePlan


class FinishedProcess:
    def __init__(self, returncode=0, *, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.terminated = False
        self.killed = False

    def communicate(self, timeout=None):
        return self.stdout, self.stderr

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class RunningProcess(FinishedProcess):
    def __init__(self):
        super().__init__(None)

    def communicate(self, timeout=None):
        if self.terminated or self.killed:
            return "", ""
        raise subprocess.TimeoutExpired(("worker",), timeout)


def generation_inputs(root, *, backend="pocket-tts", model=None):
    identity = "a" * 64
    directory = root / f"generation-input-{identity[:16]}"
    directory.mkdir(parents=True)
    queue = directory / "queue.jsonl"
    voices = directory / "voice-manifest.json"
    queue.write_text("queue", encoding="utf-8")
    voices.write_text("voices", encoding="utf-8")
    generation_input = PregenerationInput(
        identity=identity,
        directory=directory,
        story_index=directory / "story-index.jsonl",
        voice_manifest=voices,
        queue=queue,
        queue_sha256="b" * 64,
        queue_items=3,
        ready_items=3,
        narrator_fallback_roles=("Hotelier", "Poacher"),
    )
    plan = VoicePlan(
        job_id="c" * 24,
        created_at="2026-08-31T00:00:00+00:00",
        story_index_sha256="d" * 64,
        voice_manifest=None,
        voice_manifest_sha256=None,
        synthesis_backend=backend,
        synthesis_model=model,
        synthesis_language="en",
        synthesis_profile="stable",
        synthesis_controls_sha256="e" * 64,
        groups=(),
    )
    return generation_input, plan


class OfflineGenerationWorkerTest(unittest.TestCase):
    def test_runs_exact_private_inputs_and_reports_terminal_counts(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            generation_input, plan = generation_inputs(
                root, backend="moss-tts", model="model-id"
            )
            output = generation_input.directory.parent / (
                f"generation-output-{generation_input.identity[:16]}"
            )
            output.mkdir()
            (output / "generation-state.json").write_text("{}", encoding="utf-8")
            (output / "manifest.json").write_text("{}", encoding="utf-8")
            popen = Mock(return_value=FinishedProcess())
            worker = OfflineGenerationWorker(
                command=("vntts-worker",), popen_factory=popen
            )
            state = {
                "items": {
                    "one": {"status": "generated"},
                    "two": {"status": "failed"},
                    "three": {"status": "live_fallback"},
                }
            }

            with patch(
                "vntts.pregeneration_generation.load_generation_state",
                return_value=state,
            ):
                result = worker.generate(generation_input, plan)

        arguments = popen.call_args.args[0]
        self.assertEqual(arguments[:2], ("vntts-worker", "generate"))
        self.assertIn(str(generation_input.queue), arguments)
        self.assertIn("model-id", arguments)
        self.assertIn("2", arguments)
        self.assertEqual(arguments.count("--narrator-fallback-role"), 2)
        self.assertEqual(result.generated, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.other_terminal, 1)
        self.assertEqual(result.total, 3)

    def test_pocket_generation_uses_one_unseeded_attempt(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, plan = generation_inputs(Path(temporary_directory))
            process = FinishedProcess(2, stderr="details\nmodel unavailable\n")
            popen = Mock(return_value=process)

            with self.assertRaisesRegex(OfflineGenerationError, "model unavailable"):
                OfflineGenerationWorker(
                    command=("worker",), popen_factory=popen
                ).generate(generation_input, plan)

        arguments = popen.call_args.args[0]
        retries = arguments.index("--retries")
        self.assertEqual(arguments[retries + 1], "0")

    def test_cancellation_terminates_only_the_owned_worker(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, plan = generation_inputs(Path(temporary_directory))
            process = RunningProcess()
            cancellation = Event()
            cancellation.set()

            with self.assertRaises(OfflineGenerationCancelled):
                OfflineGenerationWorker(
                    command=("worker",), popen_factory=Mock(return_value=process)
                ).generate(generation_input, plan, cancellation)

        self.assertFalse(process.terminated)
        self.assertFalse(process.killed)

    def test_running_cancellation_terminates_the_owned_worker(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, plan = generation_inputs(Path(temporary_directory))
            process = RunningProcess()
            cancellation = Mock()
            cancellation.is_set.side_effect = (False, True)

            with self.assertRaises(OfflineGenerationCancelled):
                OfflineGenerationWorker(
                    command=("worker",), popen_factory=Mock(return_value=process)
                ).generate(generation_input, plan, cancellation)

        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)

    def test_frozen_app_uses_hidden_generation_worker(self):
        worker = OfflineGenerationWorker()

        with patch.object(sys, "frozen", True, create=True):
            command = worker.command()

        self.assertEqual(command, (sys.executable, "--offline-generation-worker"))


if __name__ == "__main__":
    unittest.main()

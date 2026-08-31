"""Cancellable subprocess boundary for resumable self-service generation."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_voices import VoicePlan


class OfflineGenerationError(RuntimeError):
    """The private generation input could not reach a terminal worker state."""


class OfflineGenerationCancelled(OfflineGenerationError):
    """The player cancelled the exact owned generation worker."""


@dataclass(frozen=True)
class OfflineGenerationResult:
    output: Path
    state: Path
    manifest: Path
    generated: int
    failed: int
    other_terminal: int

    @property
    def total(self):
        return self.generated + self.failed + self.other_terminal


class OfflineGenerationWorker:
    def __init__(self, *, command=None, popen_factory=subprocess.Popen):
        self._configured_command = tuple(command) if command else None
        self.popen_factory = popen_factory

    def command(self):
        if self._configured_command:
            return self._configured_command
        if getattr(sys, "frozen", False):
            return (sys.executable, "--offline-generation-worker")
        return (sys.executable, "-m", "vntts.authoring.cli")

    def generate(self, generation_input, voice_plan, cancel_event=None):
        output = _generation_output(generation_input)
        arguments = self._base_arguments(generation_input, voice_plan, output)
        return self._execute(
            arguments,
            generation_input,
            output,
            cancel_event=cancel_event,
        )

    def repair(
        self,
        generation_input,
        voice_plan,
        generation_result,
        *,
        action,
        queue_ids,
        cancel_event=None,
    ):
        """Apply one exact, typed repair batch to the resumable output."""
        if not isinstance(generation_result, OfflineGenerationResult):
            raise OfflineGenerationError("Offline generation result is invalid")
        output = _generation_output(generation_input)
        if generation_result.output.resolve() != output.resolve():
            raise OfflineGenerationError("Offline repair output identity changed")
        option, retries = _repair_option(action)
        queue_ids = _queue_ids(queue_ids)
        arguments = self._base_arguments(
            generation_input,
            voice_plan,
            output,
            retries=retries,
        )
        for queue_id in queue_ids:
            arguments.extend(("--queue-id", queue_id))
            if option is not None:
                arguments.extend((option, queue_id))
        return self._execute(
            arguments,
            generation_input,
            output,
            cancel_event=cancel_event,
        )

    def inspect(self, generation_input):
        """Reload the current validated terminal counts without starting work."""
        return _load_result(_generation_output(generation_input), generation_input)

    def _base_arguments(self, generation_input, voice_plan, output, *, retries=None):
        if not isinstance(generation_input, PregenerationInput):
            raise OfflineGenerationError("Offline generation input is invalid")
        if not isinstance(voice_plan, VoicePlan):
            raise OfflineGenerationError("Offline voice plan is invalid")
        if not generation_input.directory.name.endswith(
            generation_input.identity[:16]
        ):
            raise OfflineGenerationError("Offline generation input identity changed")
        if retries is None:
            retries = 0 if voice_plan.synthesis_backend == "pocket-tts" else 2
        generation_profile = (
            "default"
            if voice_plan.synthesis_backend == "pocket-tts"
            else voice_plan.synthesis_profile
        )
        arguments = [
            *self.command(),
            "generate",
            "--queue",
            str(generation_input.queue),
            "--output",
            str(output),
            "--voice-manifest",
            str(generation_input.voice_manifest),
            "--backend",
            voice_plan.synthesis_backend,
            "--generation-profile",
            generation_profile,
            "--narrator-character",
            "Narrator",
            "--retries",
            str(retries),
        ]
        if voice_plan.synthesis_model:
            arguments.extend(("--model", voice_plan.synthesis_model))
        for role in generation_input.narrator_fallback_roles:
            arguments.extend(("--narrator-fallback-role", role))
        return arguments

    def _execute(
        self,
        arguments,
        generation_input,
        output,
        *,
        cancel_event=None,
    ):
        if cancel_event is not None and cancel_event.is_set():
            raise OfflineGenerationCancelled("Offline speech generation was cancelled")
        try:
            process = self.popen_factory(
                tuple(arguments),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as error:
            raise OfflineGenerationError(
                f"Unable to start offline speech generation: {error}"
            ) from error

        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    _terminate_process(process)
                    raise OfflineGenerationCancelled(
                        "Offline speech generation was cancelled"
                    )
        if process.returncode:
            detail = _last_output_line(stderr) or _last_output_line(stdout)
            raise OfflineGenerationError(
                "Offline speech could not be generated"
                + (f": {detail}" if detail else ".")
            )
        return _load_result(output, generation_input)


def _generation_output(generation_input):
    if not isinstance(generation_input, PregenerationInput):
        raise OfflineGenerationError("Offline generation input is invalid")
    return generation_input.directory.parent / (
        f"generation-output-{generation_input.identity[:16]}"
    )


def _load_result(output, generation_input):
    state_path = output / "generation-state.json"
    manifest_path = output / "manifest.json"
    if not state_path.is_file() or not manifest_path.is_file():
        raise OfflineGenerationError(
            "Offline generation finished without publishing its result"
        )
    try:
        state = load_generation_state(state_path, generation_input.queue)
    except (BulkGenerationError, OSError, ValueError) as error:
        raise OfflineGenerationError(
            f"Offline generation result is invalid: {error}"
        ) from error
    counts = {"generated": 0, "failed": 0, "other": 0}
    for item in state.get("items", {}).values():
        status = item.get("status") if isinstance(item, dict) else None
        if status in {"generated", "approved"}:
            counts["generated"] += 1
        elif status == "failed":
            counts["failed"] += 1
        else:
            counts["other"] += 1
    return OfflineGenerationResult(
        output=output,
        state=state_path,
        manifest=manifest_path,
        generated=counts["generated"],
        failed=counts["failed"],
        other_terminal=counts["other"],
    )


def _queue_ids(values):
    if not isinstance(values, (tuple, list)) or not values:
        raise OfflineGenerationError("Offline repair requires exact queue IDs")
    result = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise OfflineGenerationError("Offline repair queue ID is invalid")
        result.append(value)
    if len(result) != len(set(result)):
        raise OfflineGenerationError("Offline repair queue IDs are duplicated")
    return tuple(sorted(result))


def _repair_option(action):
    options = {
        "safe_resume": (None, 0),
        "sentence_boundary_segmentation": ("--sentence-segment-failed", 0),
        "edge_silence_trim": ("--trim-edge-silence-failed", 0),
        "bounded_seed_retry": ("--bounded-seed-failed", 2),
    }
    try:
        option, retries = options[action]
    except KeyError as error:
        raise OfflineGenerationError(
            f"Offline repair action is unsupported: {action!r}"
        ) from error
    return option, retries


def _terminate_process(process):
    process.terminate()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def _last_output_line(value):
    if not isinstance(value, str):
        return None
    return next(
        (line.strip() for line in reversed(value.splitlines()) if line.strip()), None
    )


__all__ = [
    "OfflineGenerationCancelled",
    "OfflineGenerationError",
    "OfflineGenerationResult",
    "OfflineGenerationWorker",
]

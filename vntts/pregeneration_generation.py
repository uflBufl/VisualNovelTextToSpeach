"""Cancellable subprocess boundary for resumable self-service generation."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_voices import VoicePlan
from vntts.subprocess_utils import last_output_line, terminate_process


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
    pending_review: int = 0

    @property
    def total(self):
        return self.generated + self.failed + self.other_terminal


@dataclass(frozen=True)
class OfflineGenerationProgress:
    generated: int = 0
    failed: int = 0
    other_terminal: int = 0
    active_phase: str | None = None

    @property
    def completed(self):
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
        try:
            current = self.inspect(generation_input)
        except OfflineGenerationError:
            pass
        else:
            if current.total == generation_input.ready_items:
                return current
        output = _generation_output(generation_input)
        arguments = self._base_arguments(generation_input, voice_plan, output)
        result = self._execute(
            arguments,
            generation_input,
            output,
            cancel_event=cancel_event,
        )
        if not generation_input.audio_event_projection_queue_ids:
            return result
        projection_arguments = self._base_arguments(
            generation_input, voice_plan, output
        )
        for queue_id in generation_input.audio_event_projection_queue_ids:
            projection_arguments.extend(("--queue-id", queue_id))
            projection_arguments.extend(("--audio-event-spoken-projection", queue_id))
        return self._execute(
            projection_arguments,
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
        projection_ids = set(generation_input.audio_event_projection_queue_ids)
        current = generation_result
        for selected, is_projection in (
            (tuple(value for value in queue_ids if value not in projection_ids), False),
            (tuple(value for value in queue_ids if value in projection_ids), True),
        ):
            if not selected:
                continue
            arguments = self._base_arguments(
                generation_input,
                voice_plan,
                output,
                retries=retries,
            )
            for queue_id in selected:
                arguments.extend(("--queue-id", queue_id))
                if option is not None:
                    arguments.extend((option, queue_id))
                if is_projection:
                    arguments.extend(("--audio-event-spoken-projection", queue_id))
            current = self._execute(
                arguments,
                generation_input,
                output,
                cancel_event=cancel_event,
            )
        return current

    def inspect(self, generation_input):
        """Reload the current validated terminal counts without starting work."""
        return _load_result(_generation_output(generation_input), generation_input)

    def inspect_progress(self, generation_input):
        """Reload durable per-item progress while generation is still running."""
        output = _generation_output(generation_input)
        state_path = output / "generation-state.json"
        if not state_path.is_file():
            return OfflineGenerationProgress()
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeError) as error:
            raise OfflineGenerationError(
                f"Unable to inspect offline generation progress: {error}"
            ) from error
        if (
            not isinstance(state, dict)
            or state.get("queue_sha256") != generation_input.queue_sha256
            or not isinstance(state.get("items"), dict)
        ):
            raise OfflineGenerationError("Offline generation progress is invalid")
        generated = failed = other_terminal = 0
        for item in state["items"].values():
            status = item.get("status") if isinstance(item, dict) else None
            if status in {"generated", "approved"}:
                generated += 1
            elif status == "failed":
                failed += 1
            elif status in {"live_fallback", "omitted", "not_reproducible"}:
                other_terminal += 1
        active = state.get("active")
        return OfflineGenerationProgress(
            generated=generated,
            failed=failed,
            other_terminal=other_terminal,
            active_phase=(
                str(active.get("phase"))
                if isinstance(active, dict) and active.get("phase")
                else None
            ),
        )

    def _base_arguments(self, generation_input, voice_plan, output, *, retries=None):
        if not isinstance(generation_input, PregenerationInput):
            raise OfflineGenerationError("Offline generation input is invalid")
        if not isinstance(voice_plan, VoicePlan):
            raise OfflineGenerationError("Offline voice plan is invalid")
        if not generation_input.directory.name.endswith(generation_input.identity[:16]):
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
            "--cache-directory",
            str(_synthesis_cache_directory(generation_input)),
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
                    terminate_process(process)
                    raise OfflineGenerationCancelled(
                        "Offline speech generation was cancelled"
                    )
        if process.returncode:
            detail = last_output_line(stderr) or last_output_line(stdout)
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


def _synthesis_cache_directory(generation_input):
    job_directory = generation_input.directory.parent
    name = job_directory.name
    is_job_identity = len(name) == 24 and all(
        character in "0123456789abcdef" for character in name
    )
    root = job_directory.parent if is_job_identity else job_directory
    return root / "synthesis-cache"


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
    generated, failed, other_terminal = _terminal_counts(state)
    pending = 0
    for item in state.get("items", {}).values():
        if isinstance(item, dict) and item.get("review_status") == "pending_review":
            pending += 1
    return OfflineGenerationResult(
        output=output,
        state=state_path,
        manifest=manifest_path,
        generated=generated,
        failed=failed,
        other_terminal=other_terminal,
        pending_review=pending,
    )


def _terminal_counts(state):
    counts = {"generated": 0, "failed": 0, "other": 0}
    for item in state.get("items", {}).values():
        status = item.get("status") if isinstance(item, dict) else None
        if status in {"generated", "approved"}:
            counts["generated"] += 1
        elif status == "failed":
            counts["failed"] += 1
        else:
            counts["other"] += 1
    return counts["generated"], counts["failed"], counts["other"]


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
        "offline_fallback_backend": ("--offline-fallback-failed", 0),
    }
    try:
        option, retries = options[action]
    except KeyError as error:
        raise OfflineGenerationError(
            f"Offline repair action is unsupported: {action!r}"
        ) from error
    return option, retries


__all__ = [
    "OfflineGenerationCancelled",
    "OfflineGenerationError",
    "OfflineGenerationProgress",
    "OfflineGenerationResult",
    "OfflineGenerationWorker",
]

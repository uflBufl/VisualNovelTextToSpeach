"""Cancellable subprocess boundary for resumable self-service generation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from vntts_artifacts.story_index import StoryIndexError, load_story_index_document

from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
from vntts.authoring.generation_manifest import RUNTIME_PROGRESS_MANIFEST_NAME
from vntts.chapter_voice_preload import (
    _source_audio_covers_full_line,
    _validated_source_audio_line_ids,
)
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_setup import (
    PregenerationSetupError,
    estimate_generation_resources,
)
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
    available: bool = True
    runtime_status: str | None = None
    ready_line_ids: tuple[str, ...] = ()

    @property
    def completed(self):
        return self.generated + self.failed + self.other_terminal


class OfflineGenerationWorker:
    def __init__(
        self,
        *,
        command=None,
        popen_factory=subprocess.Popen,
        backend_factory=None,
    ):
        self._configured_command = tuple(command) if command else None
        self.popen_factory = popen_factory
        self.backend_factory = backend_factory
        self._process = None
        self._in_process_active = False
        self._startup_status = None
        self._disk_checked_inputs = set()

    def command(self):
        if self._configured_command:
            return self._configured_command
        if getattr(sys, "frozen", False):
            return (sys.executable, "--offline-generation-worker")
        return (sys.executable, "-m", "vntts.authoring.cli")

    def generate(
        self, generation_input, voice_plan, cancel_event=None, *, queue_ids=None
    ):
        try:
            current = self.inspect(generation_input)
        except OfflineGenerationError:
            pass
        else:
            if current.total == generation_input.ready_items:
                return current
        if generation_input.identity not in self._disk_checked_inputs:
            _ensure_remaining_disk_space(generation_input)
            self._disk_checked_inputs.add(generation_input.identity)
        output = _generation_output(generation_input)
        selected = None if queue_ids is None else _queue_ids(queue_ids)
        projection_ids = set(generation_input.audio_event_projection_queue_ids)
        ordinary = (
            None
            if selected is None
            else tuple(
                queue_id for queue_id in selected if queue_id not in projection_ids
            )
        )
        result = None
        if ordinary is None or ordinary:
            arguments = self._base_arguments(generation_input, voice_plan, output)
            for queue_id in ordinary or ():
                arguments.extend(("--queue-id", queue_id))
            result = self._execute(
                arguments,
                generation_input,
                output,
                cancel_event=cancel_event,
            )
        projections = (
            generation_input.audio_event_projection_queue_ids
            if selected is None
            else tuple(queue_id for queue_id in selected if queue_id in projection_ids)
        )
        if projections:
            arguments = self._base_arguments(generation_input, voice_plan, output)
            for queue_id in projections:
                arguments.extend(("--queue-id", queue_id))
                arguments.extend(("--audio-event-spoken-projection", queue_id))
            result = self._execute(
                arguments,
                generation_input,
                output,
                cancel_event=cancel_event,
            )
        if result is None:
            return self.inspect(generation_input)
        return result

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
        _ensure_remaining_disk_space(generation_input)
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
            return OfflineGenerationProgress(
                available=False,
                runtime_status=self._startup_status
                if self._in_process_active
                else None,
            )
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
        runtime_status = None
        process = self._process
        if isinstance(active, dict) and (
            (
                process is not None
                and active.get("runtime_worker_pid") == process.pid
                and process.poll() is None
            )
            or (
                self._in_process_active
                and active.get("runtime_worker_pid") == os.getpid()
            )
        ):
            reported = active.get("runtime_status")
            if isinstance(reported, str) and reported.strip():
                runtime_status = reported[:2000]
        return OfflineGenerationProgress(
            generated=generated,
            failed=failed,
            other_terminal=other_terminal,
            runtime_status=runtime_status,
            active_phase=(
                str(active.get("phase"))
                if isinstance(active, dict) and active.get("phase")
                else None
            ),
            ready_line_ids=_ready_line_ids(
                generation_input.story_index,
                state["items"],
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
        if voice_plan.pocket_voice_cloning:
            arguments.append("--allow-gated-model-access")
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
        if self.backend_factory is not None and "--backend" in arguments:
            from vntts.authoring.cli import create_parser
            from vntts.authoring.cli_generation import run_generation

            command_index = arguments.index("generate")
            parsed = create_parser().parse_args(arguments[command_index:])
            if parsed.backend != "moss-tts":
                parsed = None
        else:
            parsed = None
        if parsed is not None:
            self._in_process_active = True
            self._startup_status = "Starting OpenMOSS..."
            try:
                run_generation(
                    parsed,
                    backend_factory=self.backend_factory,
                    cancellation=cancel_event,
                    startup_progress=self._set_startup_status,
                )
            except Exception as error:
                if cancel_event is not None and cancel_event.is_set():
                    raise OfflineGenerationCancelled(
                        "Offline speech generation was cancelled"
                    ) from error
                raise OfflineGenerationError(
                    f"Offline speech could not be generated: {error}"
                ) from error
            finally:
                self._in_process_active = False
                self._startup_status = None
            if cancel_event is not None and cancel_event.is_set():
                raise OfflineGenerationCancelled(
                    "Offline speech generation was cancelled"
                )
            return _load_result(output, generation_input)
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

        self._process = process
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if cancel_event is not None and cancel_event.is_set():
                        terminate_process(process)
                        raise OfflineGenerationCancelled(
                            "Offline speech generation was cancelled"
                        ) from None
        finally:
            self._process = None
        if process.returncode:
            detail = last_output_line(stderr) or last_output_line(stdout)
            raise OfflineGenerationError(
                "Offline speech could not be generated"
                + (f": {detail}" if detail else ".")
            )
        return _load_result(output, generation_input)

    def _set_startup_status(self, message):
        if isinstance(message, str) and message.strip():
            self._startup_status = message.strip()[:2000]


def _ensure_remaining_disk_space(generation_input):
    try:
        estimate = estimate_generation_resources(generation_input)
    except PregenerationSetupError:
        # The worker will still validate malformed legacy inputs. Without their
        # queue text there is no honest storage estimate to enforce here.
        return
    if estimate.remaining_disk_bytes == 0:
        return
    required = estimate.remaining_disk_bytes + 1_048_576
    free = shutil.disk_usage(generation_input.directory.parent).free
    if free < required:
        raise OfflineGenerationError(
            "Not enough free disk space to generate the remaining offline audio: "
            f"need about {_megabytes(required)} MB, have {_megabytes(free)} MB. "
            "Free space or choose fewer stories, then retry; saved work stays."
        )


def _megabytes(value):
    return max(1, (value + 999_999) // 1_000_000)


def _generation_output(generation_input):
    if not isinstance(generation_input, PregenerationInput):
        raise OfflineGenerationError("Offline generation input is invalid")
    return generation_input.directory.parent / (
        f"generation-output-{generation_input.identity[:16]}"
    )


def runtime_progress_manifest_path(generation_input: PregenerationInput) -> Path:
    """Return the temporary manifest published while this input is generating."""
    return _generation_output(generation_input) / RUNTIME_PROGRESS_MANIFEST_NAME


@lru_cache(maxsize=16)
def _static_ready_line_ids(story_index: Path) -> tuple[str, ...]:
    """Return immutable source/non-spoken routes that need no generated WAV."""
    try:
        story = load_story_index_document(story_index)
    except OSError, StoryIndexError, TypeError, ValueError:
        return ()
    completion = story.metadata.get("source_audio_completion")
    authoritative_line_ids = _validated_source_audio_line_ids(story_index, story)
    return tuple(
        record.line_id
        for record in story.records
        if not record.speakable
        or (
            record.line_id in authoritative_line_ids
            and _source_audio_covers_full_line(
                record.document,
                completion_contract=completion,
                semantic_authorized=True,
            )
        )
    )


def _ready_line_ids(
    story_index: Path,
    items: Mapping[object, object],
) -> tuple[str, ...]:
    ready_line_ids = set(_static_ready_line_ids(story_index))
    for item in items.values():
        if not isinstance(item, dict) or item.get("status") not in {
            "generated",
            "approved",
        }:
            continue
        line_id = item.get("line_id")
        if isinstance(line_id, str) and line_id:
            ready_line_ids.add(line_id)
    return tuple(sorted(ready_line_ids))


def validate_offline_generation_result(
    generation_input, generation_result, action, *, error_type=OfflineGenerationError
):
    """Bind a typed generation result to its one deterministic output directory."""
    if not isinstance(generation_input, PregenerationInput):
        raise error_type("Offline generation input is invalid")
    if not isinstance(generation_result, OfflineGenerationResult):
        raise error_type("Offline generation result is invalid")
    if (
        generation_result.output.resolve()
        != _generation_output(generation_input).resolve()
    ):
        raise error_type(f"Offline {action} output identity changed")


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
    "runtime_progress_manifest_path",
    "validate_offline_generation_result",
]

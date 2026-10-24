"""Cancellable subprocess boundary for resumable self-service generation."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from threading import Lock, get_ident
from typing import Protocol, cast

from vntts_artifacts.story_index import StoryIndexError

from vntts.authoring.bulk_generation import (
    GenerationProgressEvent,
    GenerationProgressItem,
    _assert_control_files_unchanged,
    _GenerationControl,
    capture_generation_cancellation,
    reconcile_generation_cancellation,
    snapshot_generation_control_files,
)
from vntts.authoring.generation_lease import (
    BulkGenerationError,
)
from vntts.authoring.generation_manifest import RUNTIME_PROGRESS_MANIFEST_NAME
from vntts.authoring.generation_state import (
    generation_item_status,
    load_generation_state,
)
from vntts.chapter_voice_preload import (
    _has_authoritative_source_audio,
    _source_audio_evidence_payload,
    _validated_source_audio_line_ids,
)
from vntts.cleanup import cleanup_on_exit
from vntts.document_identity import sha256_file
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.generation_database import (
    GenerationProgress,
    read_progress,
    require_registered_output,
)
from vntts.generation_database import state_path as generation_state_path
from vntts.generation_stall import GenerationStallWatchdog
from vntts.moss_cpp_backend import moss_cpp_requested
from vntts.pregeneration_contract import (
    OfflineGenerationCancelled as OfflineGenerationCancelled,
)
from vntts.pregeneration_contract import (
    OfflineGenerationError as OfflineGenerationError,
)
from vntts.pregeneration_contract import (
    OfflineGenerationResult as OfflineGenerationResult,
)
from vntts.pregeneration_queue import PregenerationInput, _recording_fingerprint
from vntts.pregeneration_setup import (
    PregenerationSetupError,
    estimate_generation_resources,
    load_verified_story_index_document,
)
from vntts.pregeneration_voices import VoicePlan
from vntts.speech_backend_runtime import shutdown_speech_backend
from vntts.speech_worker import RetainedWorkerRuntime, _RetainedBackend
from vntts.subprocess_utils import last_output_line, terminate_running_process
from vntts.voices import CharacterVoiceRegistry


class _Cancellation(Protocol):
    def is_set(self) -> bool: ...


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
    def completed(self) -> int:
        return self.generated + self.failed + self.other_terminal


class _IncrementalGenerationProgress:
    def __init__(self) -> None:
        self.items: dict[str, GenerationProgressItem] = {}
        self.counts: Counter[str] = Counter()
        self.static_ready: frozenset[str] = frozenset()
        self.source_stamp: tuple[tuple[int, ...] | None, ...] | None = None
        self.ready: Counter[str] = Counter()
        self._ready_line_ids: tuple[str, ...] | None = None

    def update(self, item: GenerationProgressItem) -> None:
        previous = self.items.get(item.queue_id)
        if previous == item:
            return
        for value, delta in ((previous, -1), (item, 1)):
            if value is None:
                continue
            kind = (
                "generated"
                if value.status in {"generated", "approved"}
                else (
                    "other"
                    if value.status in {"live_fallback", "omitted", "not_reproducible"}
                    else value.status
                )
            )
            if kind is not None:
                self.counts[kind] += delta
            if kind == "generated" and value.line_id is not None:
                line_id = value.line_id
                was_ready = self.ready[line_id] > 0
                self.ready[line_id] += delta
                if self.ready[line_id] == 0:
                    del self.ready[line_id]
                if (
                    was_ready != (self.ready[line_id] > 0)
                    and line_id not in self.static_ready
                ):
                    self._ready_line_ids = None
        self.items[item.queue_id] = item

    def set_static_ready(self, values: tuple[str, ...]) -> None:
        ready = frozenset(values)
        if ready != self.static_ready:
            self.static_ready = ready
            self._ready_line_ids = None

    @property
    def ready_line_ids(self) -> tuple[str, ...]:
        if self._ready_line_ids is None:
            self._ready_line_ids = tuple(sorted(self.static_ready | self.ready.keys()))
        return self._ready_line_ids


class _GenerationBackendSession:
    """Retain an isolated model only for one serial generation/recovery run."""

    def __init__(
        self,
        generation_input: PregenerationInput,
        voice_plan: VoicePlan,
        factory: Callable[..., object],
    ) -> None:
        self.generation_input = generation_input
        self.voice_plan = voice_plan
        self.owner_thread = get_ident()
        self.closed = False
        self.cache_root = _synthesis_cache_directory(generation_input).resolve()
        self.original_factory = factory
        self.controls: list[_GenerationControl] | None = None
        self.runtime = RetainedWorkerRuntime(
            "moss-tts", backend_factory=self._create_backend
        )

    def validate(
        self, generation_input: PregenerationInput, voice_plan: VoicePlan
    ) -> None:
        if (
            self.closed
            or get_ident() != self.owner_thread
            or generation_input != self.generation_input
            or voice_plan != self.voice_plan
        ):
            raise OfflineGenerationError("Offline generation session identity changed")

    def shutdown(self) -> None:
        self.closed = True
        self.runtime.shutdown()

    def _create_backend(
        self, registry: CharacterVoiceRegistry, **options: object
    ) -> _RetainedBackend:
        return cast(
            _RetainedBackend,
            self.original_factory("moss-tts", registry, self.cache_root, **options),
        )

    def _admit_controls(
        self, registry: CharacterVoiceRegistry, narrator_reference: object
    ) -> None:
        references = {
            Path(path).resolve()
            for voice in registry.unique_voices()
            for path in voice.references
        }
        if isinstance(narrator_reference, (str, Path)):
            references.add(Path(narrator_reference).expanduser().resolve())
        paths: dict[str, str | Path | tuple[str | Path, str]] = {
            "voice_manifest": (
                self.generation_input.voice_manifest,
                self.generation_input.voice_manifest_sha256,
            ),
            **{
                f"voice_reference:{index:04d}": path
                for index, path in enumerate(sorted(references))
            },
        }
        if self.controls is None:
            self.controls = snapshot_generation_control_files(paths)
            return
        if {Path(path).resolve() for path in references} | {
            self.generation_input.voice_manifest.resolve()
        } != {Path(control["path"]) for control in self.controls}:
            raise OfflineGenerationError("Offline generation control inventory changed")
        _assert_control_files_unchanged(self.controls)

    def factory(
        self,
        name: str,
        registry: CharacterVoiceRegistry,
        cache_root: str | Path,
        **options: object,
    ) -> object:
        self.validate(self.generation_input, self.voice_plan)
        if Path(cache_root).resolve() != self.cache_root:
            raise OfflineGenerationError("Offline generation cache identity changed")
        if name != "moss-tts" or moss_cpp_requested(options.get("model_name")):
            return self.original_factory(name, registry, cache_root, **options)
        self._admit_controls(registry, options.get("narrator_reference"))
        return self.runtime(registry, **options)


class OfflineGenerationWorker:
    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        backend_factory: Callable[..., object] | None = None,
        retain_isolated_backend: bool = False,
    ) -> None:
        if not isinstance(retain_isolated_backend, bool):
            raise TypeError("retain_isolated_backend must be boolean")
        self.retain_isolated_backend = retain_isolated_backend
        self._configured_command = tuple(command) if command else None
        self.popen_factory = popen_factory
        self.backend_factory = backend_factory
        self._process: subprocess.Popen[str] | None = None
        self._in_process_active = False
        self._startup_status: str | None = None
        self._progress_lock = Lock()
        self._memory_progress_enabled = backend_factory is not None
        self._live_progress: tuple[str, OfflineGenerationProgress] | None = None
        self._incremental_progress: (
            tuple[str, _IncrementalGenerationProgress] | None
        ) = None
        self._disk_checked_inputs: set[str] = set()
        self._backend_session: _GenerationBackendSession | None = None
        self._backend_session_lock = Lock()

    @contextmanager
    def generation_session(
        self, generation_input: PregenerationInput, voice_plan: VoicePlan
    ) -> Iterator[None]:
        """Keep MLX startup outside the per-line durable recovery boundary."""
        if (
            not self.retain_isolated_backend
            or self.backend_factory is None
            or voice_plan.synthesis_backend != "moss-tts"
        ):
            yield
            return
        if not self._backend_session_lock.acquire(blocking=False):
            raise OfflineGenerationError("Offline generation session is already active")
        primary_error: BaseException | None = None
        session = None
        try:
            _preflight_generation_authority(generation_input)
            session = _GenerationBackendSession(
                generation_input, voice_plan, self.backend_factory
            )
            self._backend_session = session
            yield
        except BaseException as error:
            primary_error = error
            raise
        finally:
            self._backend_session = None
            try:
                if session is not None:
                    shutdown_speech_backend(session, primary_error=primary_error)
            finally:
                self._backend_session_lock.release()

    def _check_generation_session(
        self, generation_input: PregenerationInput, voice_plan: VoicePlan
    ) -> None:
        if self._backend_session is not None:
            self._backend_session.validate(generation_input, voice_plan)
        elif self._backend_session_lock.locked():
            raise OfflineGenerationError("Offline generation session is closing")

    def command(self) -> tuple[str, ...]:
        if self._configured_command:
            return self._configured_command
        if getattr(sys, "frozen", False):
            return (sys.executable, "--offline-generation-worker")
        return (sys.executable, "-m", "vntts.authoring.cli_generation")

    def generate(
        self,
        generation_input: PregenerationInput,
        voice_plan: VoicePlan,
        cancel_event: _Cancellation | None = None,
        *,
        queue_ids: object = None,
    ) -> OfflineGenerationResult:
        self._check_generation_session(generation_input, voice_plan)
        _preflight_generation_authority(generation_input)
        if queue_ids is None:
            with suppress(OfflineGenerationError):
                current = self.inspect(generation_input)
                expected_terminal = generation_input.ready_items + len(
                    generation_input.audio_event_omission_queue_ids
                )
                if current.total == expected_terminal:
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
        result: OfflineGenerationResult | None = None
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
        generation_input: PregenerationInput,
        voice_plan: VoicePlan,
        generation_result: OfflineGenerationResult,
        *,
        action: str,
        queue_ids: object,
        cancel_event: _Cancellation | None = None,
    ) -> OfflineGenerationResult:
        """Apply one exact, typed repair batch to the resumable output."""
        self._check_generation_session(generation_input, voice_plan)
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
        resumed_items = (
            _state_items(
                load_generation_state(generation_result.state, generation_input.queue)
            )
            if action == "safe_resume"
            else {}
        )
        batches = (
            tuple(((value,), value in projection_ids) for value in queue_ids)
            if action == "safe_resume"
            else (
                (
                    tuple(value for value in queue_ids if value not in projection_ids),
                    False,
                ),
                (tuple(value for value in queue_ids if value in projection_ids), True),
            )
        )
        for selected, is_projection in batches:
            if not selected:
                continue
            resume_options: list[str] = []
            if action == "safe_resume":
                saved = resumed_items.get(selected[0], {})
                repair = saved.get("failure_repair")
                if generation_item_status(saved) == "cancelled" and isinstance(
                    repair, dict
                ):
                    option, retries = _repair_option(str(repair["strategy"]))
                    if repair["strategy"] == "sentence_boundary_segmentation":
                        resume_options = ["--segment-pause-ms", str(repair["pause_ms"])]
                    elif repair["strategy"] == "inline_pause_marker":
                        resume_options = ["--inline-pause-ms", str(repair["pause_ms"])]
                else:
                    option, retries = _repair_option(action)
            arguments = self._base_arguments(
                generation_input,
                voice_plan,
                output,
                retries=retries,
            )
            arguments.extend(resume_options)
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

    def inspect(self, generation_input: PregenerationInput) -> OfflineGenerationResult:
        """Reload the current validated terminal counts without starting work."""
        return _load_result(_generation_output(generation_input), generation_input)

    def inspect_progress(
        self, generation_input: PregenerationInput
    ) -> OfflineGenerationProgress:
        """Inspect in-memory OpenMOSS progress or a subprocess's durable state."""
        with self._progress_lock:
            memory_progress = self._memory_progress_enabled
            startup_status = self._startup_status if memory_progress else None
            if (
                memory_progress
                and self._live_progress is not None
                and self._live_progress[0] == generation_input.identity
            ):
                return self._live_progress[1]
        output = _generation_output(generation_input)
        state_path = generation_state_path(output)
        if not state_path.is_file():
            return OfflineGenerationProgress(
                available=False, runtime_status=startup_status
            )
        try:
            progress = self._read_progress(state_path, generation_input)
        except (
            ValueError,
            OSError,
            UnicodeError,
            BulkGenerationError,
        ) as error:
            raise OfflineGenerationError(
                f"Unable to inspect offline generation progress: {error}"
            ) from error
        if not memory_progress:
            return progress
        progress = replace(
            progress,
            runtime_status=startup_status,
            active_phase=None,
            ready_line_ids=_durable_ready_line_ids(generation_input, progress),
        )
        with self._progress_lock:
            if (
                self._live_progress is not None
                and self._live_progress[0] == generation_input.identity
            ):
                return self._live_progress[1]
        return progress

    def _read_progress(
        self, state_path: Path, generation_input: PregenerationInput
    ) -> OfflineGenerationProgress:
        return self._progress_from_database(read_progress(state_path), generation_input)

    def _progress_from_database(
        self, snapshot: GenerationProgress, generation_input: PregenerationInput
    ) -> OfflineGenerationProgress:
        if snapshot.queue_sha256 != generation_input.queue_sha256:
            raise OfflineGenerationError("Offline generation progress is invalid")
        ready = set(
            _static_ready_line_ids(
                generation_input.story_index, generation_input.story_index_sha256
            )
        )
        if snapshot.ready_lines:
            try:
                story = load_verified_story_index_document(
                    generation_input.story_index, generation_input.story_index_sha256
                )
            except OSError, StoryIndexError, ValueError:
                pass
            else:
                candidates = set(snapshot.ready_lines)
                ready.update(
                    record.line_id
                    for record in story.records
                    if (record.line_id, record.text_sha256) in candidates
                )
        active = snapshot.active
        return OfflineGenerationProgress(
            generated=snapshot.generated,
            failed=snapshot.failed,
            other_terminal=snapshot.other_terminal,
            runtime_status=self._runtime_status(active),
            active_phase=(
                str(active.get("phase"))
                if isinstance(active, dict) and active.get("phase")
                else None
            ),
            ready_line_ids=tuple(sorted(ready)),
        )

    def _runtime_status(self, active: object) -> str | None:
        owner_pid = (
            active.get("generation_owner_pid", active.get("runtime_worker_pid"))
            if isinstance(active, dict)
            else None
        )
        process = self._process
        if isinstance(active, dict) and (
            (
                process is not None
                and owner_pid == process.pid
                and process.poll() is None
            )
            or (self._in_process_active and owner_pid == os.getpid())
        ):
            reported = active.get("runtime_status")
            if isinstance(reported, str) and reported.strip():
                return reported[:2000]
        return None

    def _base_arguments(
        self,
        generation_input: PregenerationInput,
        voice_plan: VoicePlan,
        output: Path,
        *,
        retries: int | None = None,
    ) -> list[str]:
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
            "--approve-validated-audio",
            "--narrator-character",
            "Narrator",
            "--retries",
            str(retries),
        ]
        if voice_plan.synthesis_model:
            arguments.extend(("--model", voice_plan.synthesis_model))
        if voice_plan.synthesis_language:
            arguments.extend(("--language", voice_plan.synthesis_language))
        if voice_plan.pocket_voice_cloning:
            arguments.append("--allow-gated-model-access")
        if (
            voice_plan.synthesis_backend == "coqui-xtts"
            and voice_plan.xtts_terms_accepted
        ):
            arguments.append("--accept-xtts-terms")
        for role in generation_input.narrator_fallback_roles:
            arguments.extend(("--narrator-fallback-role", role))
        return arguments

    def _execute(
        self,
        arguments: list[str],
        generation_input: PregenerationInput,
        output: Path,
        *,
        cancel_event: _Cancellation | None = None,
    ) -> OfflineGenerationResult:
        if cancel_event is not None and cancel_event.is_set():
            raise OfflineGenerationCancelled("Offline speech generation was cancelled")
        backend_factory = (
            self._backend_session.factory
            if self._backend_session is not None
            else self.backend_factory
        )
        parsed: argparse.Namespace | None
        if backend_factory is not None and "--backend" in arguments:
            from vntts.authoring.cli_generation import (
                create_generation_parser,
                run_generation,
            )

            command_index = arguments.index("generate")
            parsed = create_generation_parser().parse_args(arguments[command_index:])
            if parsed.backend != "moss-tts":
                parsed = None
        else:
            parsed = None
        if parsed is not None:
            with self._progress_lock:
                self._memory_progress_enabled = True
                self._in_process_active = True
                self._startup_status = "Starting OpenMOSS..."
                if (
                    self._live_progress is not None
                    and self._live_progress[0] != generation_input.identity
                ):
                    self._live_progress = None
            with GenerationStallWatchdog(cancel_event) as watchdog:

                def record_progress(event: GenerationProgressEvent) -> None:
                    watchdog.progress({"active": event.active})
                    self._record_progress(event, generation_input)

                try:
                    run_generation(
                        parsed,
                        backend_factory=backend_factory,
                        cancellation=cancel_event,
                        startup_progress=self._set_startup_status,
                        progress_observer=record_progress,
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
                    with self._progress_lock:
                        self._in_process_active = False
                        self._startup_status = None
                if cancel_event is not None and cancel_event.is_set():
                    raise OfflineGenerationCancelled(
                        "Offline speech generation was cancelled"
                    )
                watchdog.progress()
                return _load_result(output, generation_input)
        with GenerationStallWatchdog(cancel_event) as watchdog:
            watchdog.progress({"active": {"phase": "subprocess"}})
            self._run_subprocess(
                arguments, cancel_event, generation_input=generation_input
            )
            watchdog.progress()
            return _load_result(output, generation_input)

    def _run_subprocess(
        self,
        arguments: list[str],
        cancel_event: _Cancellation | None,
        *,
        generation_input: PregenerationInput | None = None,
    ) -> None:
        with self._progress_lock:
            self._memory_progress_enabled = False
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
        cancellation_witness = None
        terminated_owned_child = False

        def stop_owned_process() -> None:
            nonlocal terminated_owned_child
            if process.poll() is None:
                terminate_running_process(process)
                terminated_owned_child = process.poll() is not None

        try:
            with cleanup_on_exit(
                stop_owned_process,
                description="Offline generation process cleanup",
            ):
                while True:
                    try:
                        stdout, stderr = process.communicate(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        if cancel_event is not None and cancel_event.is_set():
                            cancellation_error = OfflineGenerationCancelled(
                                "Offline speech generation was cancelled"
                            )
                            if generation_input is not None:
                                try:
                                    cancellation_witness = (
                                        capture_generation_cancellation(
                                            _generation_output(generation_input),
                                            generation_input.queue,
                                            generation_input.queue_sha256,
                                            process,
                                        )
                                    )
                                except (
                                    BulkGenerationError,
                                    OSError,
                                    ValueError,
                                ) as witness_error:
                                    cancellation_error.add_note(
                                        f"Cancellation authority unavailable: {witness_error}"
                                    )
                            raise cancellation_error from None
        except OfflineGenerationCancelled as error:
            if cancellation_witness is not None and terminated_owned_child:
                try:
                    reconcile_generation_cancellation(cancellation_witness, process)
                except (BulkGenerationError, OSError, ValueError) as reconcile_error:
                    error.add_note(
                        f"Cancellation state could not be reconciled: {reconcile_error}"
                    )
            raise
        finally:
            self._process = None
        if process.returncode:
            detail = last_output_line(stderr) or last_output_line(stdout)
            raise OfflineGenerationError(
                "Offline speech could not be generated"
                + (f": {detail}" if detail else ".")
            )

    def _set_startup_status(self, message: object) -> None:
        if isinstance(message, str) and message.strip():
            with self._progress_lock:
                self._startup_status = message.strip()[:2000]

    def _record_progress(
        self, event: GenerationProgressEvent, generation_input: PregenerationInput
    ) -> None:
        if event.queue_sha256 != generation_input.queue_sha256:
            raise OfflineGenerationError("Offline generation progress is invalid")
        cached = self._incremental_progress
        if event.reset:
            accumulator = _IncrementalGenerationProgress()
            self._incremental_progress = generation_input.identity, accumulator
        elif cached is not None and cached[0] == generation_input.identity:
            accumulator = cached[1]
        else:
            raise OfflineGenerationError(
                "Offline generation progress was not initialized"
            )
        before = _progress_readiness_stamp(generation_input.story_index)
        # Windows ctime is creation time; restored mtime cannot prove unchanged bytes.
        if event.reset or sys.platform == "win32" or accumulator.source_stamp != before:
            static_ready = _static_ready_line_ids(
                generation_input.story_index, generation_input.story_index_sha256
            )
            if _progress_readiness_stamp(generation_input.story_index) != before:
                static_ready = ()
            accumulator.set_static_ready(static_ready)
            accumulator.source_stamp = before
        for item in event.items:
            accumulator.update(item)
        active = event.active
        progress = OfflineGenerationProgress(
            generated=accumulator.counts["generated"],
            failed=accumulator.counts["failed"],
            other_terminal=accumulator.counts["other"],
            runtime_status=self._runtime_status(active),
            active_phase=(
                str(active.get("phase"))
                if isinstance(active, dict) and active.get("phase")
                else None
            ),
            ready_line_ids=accumulator.ready_line_ids,
        )
        with self._progress_lock:
            self._live_progress = generation_input.identity, progress

    def refresh_progress(self, generation_input: PregenerationInput) -> None:
        """Refresh memory after a recovery decision committed outside generation."""
        with self._progress_lock:
            if not self._memory_progress_enabled:
                return
        state_path = generation_state_path(_generation_output(generation_input))
        try:
            progress = self._read_progress(state_path, generation_input)
            with self._progress_lock:
                self._live_progress = generation_input.identity, progress
        except (
            OSError,
            UnicodeError,
            ValueError,
            BulkGenerationError,
            OfflineGenerationError,
        ):
            pass


def _ensure_remaining_disk_space(generation_input: PregenerationInput) -> None:
    try:
        estimate = estimate_generation_resources(generation_input)
    except PregenerationSetupError:
        # The worker will still validate malformed inputs. Without their
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


def _megabytes(value: int) -> int:
    return max(1, (value + 999_999) // 1_000_000)


def _preflight_generation_authority(generation_input: PregenerationInput) -> None:
    path = generation_state_path(_generation_output(generation_input))
    if not path.exists():
        return
    try:
        progress = read_progress(path)
        if progress.queue_sha256 != generation_input.queue_sha256:
            raise ValueError("Generation state belongs to another queue")
    except (OSError, ValueError) as error:
        raise OfflineGenerationError(
            f"Saved generation authority is invalid: {error}"
        ) from error


def _generation_output(generation_input: PregenerationInput) -> Path:
    if not isinstance(generation_input, PregenerationInput):
        raise OfflineGenerationError("Offline generation input is invalid")
    try:
        require_registered_output(generation_input.output_directory)
    except (OSError, ValueError) as error:
        raise OfflineGenerationError(str(error)) from error
    return generation_input.output_directory


def runtime_progress_manifest_path(generation_input: PregenerationInput) -> Path:
    """Return the temporary manifest published while this input is generating."""
    return _generation_output(generation_input) / str(RUNTIME_PROGRESS_MANIFEST_NAME)


def _progress_readiness_stamp(story_index: Path) -> tuple[tuple[int, ...] | None, ...]:
    evidence = (
        story_index.expanduser()
        .resolve()
        .with_name("source-audio-semantic-evidence.json")
    )
    stamps: list[tuple[int, ...] | None] = []
    for path in (story_index, evidence):
        try:
            stamps.append(_recording_fingerprint(path.resolve()))
        except OSError:
            stamps.append(None)
    return tuple(stamps)


def _static_ready_line_ids(
    story_index: Path, story_index_sha256: str
) -> tuple[str, ...]:
    """Reuse readiness only while both story and optional evidence still match."""
    try:
        if sha256_file(story_index) != story_index_sha256:
            return ()
        evidence_sha256 = hashlib.sha256(
            _source_audio_evidence_payload(story_index)
        ).hexdigest()
        return _cached_static_ready_line_ids(
            story_index, story_index_sha256, evidence_sha256
        )
    except OSError, StoryIndexError, TypeError, ValueError:
        return ()


@lru_cache(maxsize=16)
def _cached_static_ready_line_ids(
    story_index: Path, story_index_sha256: str, evidence_sha256: str
) -> tuple[str, ...]:
    story = load_verified_story_index_document(story_index, story_index_sha256)
    evidence_payload = _source_audio_evidence_payload(story_index)
    if hashlib.sha256(evidence_payload).hexdigest() != evidence_sha256:
        raise ValueError("Story audio evidence changed while it was being read")
    completion = story.metadata.get("source_audio_completion")
    completion_contract = completion if isinstance(completion, str) else None
    authoritative_line_ids = _validated_source_audio_line_ids(
        story_index, story, evidence_payload=evidence_payload
    )
    return tuple(
        record.line_id
        for record in story.records
        if not record.speakable
        or (
            _has_authoritative_source_audio(
                record, completion_contract, authoritative_line_ids
            )
        )
    )


def _durable_ready_line_ids(
    generation_input: PregenerationInput, progress: OfflineGenerationProgress
) -> tuple[str, ...]:
    """Keep processed counts separate from admitted playback during cold startup."""
    ready = set(
        _static_ready_line_ids(
            generation_input.story_index, generation_input.story_index_sha256
        )
    )
    manifest = runtime_progress_manifest_path(generation_input)
    if not manifest.is_file():
        return tuple(sorted(ready))
    try:
        library = GeneratedAudioLibrary.load_optional(manifest)
        if (
            library is None
            or library.index.metadata.get("source_queue_sha256")
            != generation_input.queue_sha256
        ):
            return tuple(sorted(ready))
        story = load_verified_story_index_document(
            generation_input.story_index, generation_input.story_index_sha256
        )
        candidates = set(progress.ready_line_ids)
        for record in story.records:
            if (
                record.line_id in candidates
                and library.find(record.line_id, record.text_sha256) is not None
            ):
                ready.add(record.line_id)
    except OSError, StoryIndexError, TypeError, ValueError:
        return tuple(sorted(ready))
    return tuple(sorted(ready))


def validate_offline_generation_result(
    generation_input: PregenerationInput,
    generation_result: OfflineGenerationResult,
    action: str,
    *,
    error_type: type[Exception] = OfflineGenerationError,
) -> None:
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


def _synthesis_cache_directory(generation_input: PregenerationInput) -> Path:
    job_directory = Path(generation_input.directory).parent
    name = job_directory.name
    is_job_identity = len(name) == 24 and all(
        character in "0123456789abcdef" for character in name
    )
    root = job_directory.parent if is_job_identity else job_directory
    return root / "synthesis-cache"


def _load_result(
    output: Path, generation_input: PregenerationInput
) -> OfflineGenerationResult:
    state_path = generation_state_path(output)
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
    for item in _state_items(state).values():
        if item.get("review_status") == "pending_review":
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


def _state_items(
    state: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    raw_items = state.get("items")
    if not isinstance(raw_items, Mapping):
        raise OfflineGenerationError("Offline generation state items are invalid")
    result: dict[str, Mapping[str, object]] = {}
    for queue_id, item in raw_items.items():
        if not isinstance(queue_id, str) or not isinstance(item, Mapping):
            raise OfflineGenerationError("Offline generation state items are invalid")
        result[queue_id] = {
            str(key): value for key, value in item.items() if isinstance(key, str)
        }
        if len(result[queue_id]) != len(item):
            raise OfflineGenerationError(
                "Offline generation state item keys are invalid"
            )
    return result


def _terminal_counts(state: Mapping[str, object]) -> tuple[int, int, int]:
    counts = {"generated": 0, "failed": 0, "other": 0}
    for item in _state_items(state).values():
        status = generation_item_status(item)
        if status in {"generated", "approved"}:
            counts["generated"] += 1
        elif status == "failed":
            counts["failed"] += 1
        elif status in {"live_fallback", "omitted", "not_reproducible"}:
            counts["other"] += 1
    return counts["generated"], counts["failed"], counts["other"]


def _queue_ids(values: object) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or not values:
        raise OfflineGenerationError("Offline repair requires exact queue IDs")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise OfflineGenerationError("Offline repair queue ID is invalid")
        result.append(value)
    if len(result) != len(set(result)):
        raise OfflineGenerationError("Offline repair queue IDs are duplicated")
    return tuple(sorted(result))


def _repair_option(action: str) -> tuple[str | None, int]:
    options = {
        "safe_resume": (None, 0),
        "sentence_boundary_segmentation": ("--sentence-segment-failed", 0),
        "edge_silence_trim": ("--trim-edge-silence-failed", 0),
        "bounded_seed_retry": ("--bounded-seed-failed", 2),
        "inline_pause_marker": ("--inline-pause-failed", 0),
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

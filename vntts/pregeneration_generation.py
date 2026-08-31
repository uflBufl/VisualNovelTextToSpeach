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
        if not isinstance(generation_input, PregenerationInput):
            raise OfflineGenerationError("Offline generation input is invalid")
        if not isinstance(voice_plan, VoicePlan):
            raise OfflineGenerationError("Offline voice plan is invalid")
        if not generation_input.directory.name.endswith(
            generation_input.identity[:16]
        ):
            raise OfflineGenerationError("Offline generation input identity changed")
        if cancel_event is not None and cancel_event.is_set():
            raise OfflineGenerationCancelled("Offline speech generation was cancelled")
        output = generation_input.directory.parent / (
            f"generation-output-{generation_input.identity[:16]}"
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
            voice_plan.synthesis_profile,
            "--narrator-character",
            "Narrator",
            "--retries",
            "0" if voice_plan.synthesis_backend == "pocket-tts" else "2",
        ]
        if voice_plan.synthesis_model:
            arguments.extend(("--model", voice_plan.synthesis_model))
        for role in generation_input.narrator_fallback_roles:
            arguments.extend(("--narrator-fallback-role", role))
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

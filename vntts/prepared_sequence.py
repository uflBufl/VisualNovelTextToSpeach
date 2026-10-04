"""Prepare a fail-closed Reverse: 1999 live-sequence plan for selected chapters."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from os import PathLike
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol, TypeAlias

from durable_file import sha256_file
from vntts_artifacts.live_sequence import (
    LiveSequencePlan,
    LiveSequencePlanError,
    load_live_sequence_plan,
)
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document

from vntts.subprocess_utils import last_output_line, terminate_process

PathInput: TypeAlias = str | PathLike[str]
PopenFactory: TypeAlias = Callable[..., subprocess.Popen[str]]


class Cancellation(Protocol):
    def is_set(self) -> bool: ...


class PreparedSequenceError(RuntimeError):
    """The selected story chapters cannot safely use sequence-led playback."""


class PreparedSequenceCancelled(PreparedSequenceError):
    """Sequence preparation was cancelled before publishing a replacement."""


def reverse1999_live_sequence_command() -> tuple[str, ...] | None:
    """Return the installed publisher command without importing Qt."""
    executable = shutil.which("r1999-live-sequence")
    if executable:
        return (executable,)
    try:
        available = importlib.util.find_spec("r1999extractor.live_sequence") is not None
    except ImportError, ModuleNotFoundError, ValueError:
        available = False
    if not available:
        return None
    if getattr(sys, "frozen", False):
        # The desktop entry point will forward this worker flag without Qt.
        return (sys.executable, "--prepared-sequence-worker")
    return (sys.executable, "-m", "r1999extractor.live_sequence")


def prepare_reverse1999_sequence(
    story_index: PathInput,
    source_bundle: PathInput,
    chapters: Sequence[object],
    output: PathInput,
    *,
    cancellation: Cancellation | None = None,
    command: Sequence[str] | None = None,
    popen_factory: PopenFactory = subprocess.Popen,
) -> LiveSequencePlan:
    """Reuse or atomically publish a fully automatic selected-chapter plan."""
    _raise_if_cancelled(cancellation)
    story_index = _required_file(story_index, "Story index")
    source_bundle = _required_file(source_bundle, "Story source bundle")
    output = Path(output).expanduser().resolve()
    selected = _chapters(chapters)
    source_sha256 = _checksum(source_bundle, "Story source bundle")

    try:
        return _validated_plan(output, story_index, source_sha256, selected)
    except PreparedSequenceError:
        pass

    publisher = (
        tuple(command) if command is not None else reverse1999_live_sequence_command()
    )
    if not publisher:
        raise PreparedSequenceError(
            "Reverse: 1999 live-sequence publisher is not installed."
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as directory:
        candidate = Path(directory) / output.name
        arguments = [
            *publisher,
            "--story-index",
            str(story_index),
            "--bundle",
            str(source_bundle),
            "--output",
            str(candidate),
        ]
        for chapter in selected:
            arguments.extend(("--chapter", chapter))
        _run(arguments, cancellation, popen_factory)
        _raise_if_cancelled(cancellation)
        if _checksum(source_bundle, "Story source bundle") != source_sha256:
            raise PreparedSequenceError(
                "Story source bundle changed while sequence was prepared."
            )
        _validated_plan(candidate, story_index, source_sha256, selected)
        _raise_if_cancelled(cancellation)
        os.replace(candidate, output)
    return _validated_plan(output, story_index, source_sha256, selected)


def prepare_sequence_for_prepared_story(
    story_index: PathInput,
    output: PathInput,
    *,
    cancellation: Cancellation | None = None,
    command: Sequence[str] | None = None,
    popen_factory: PopenFactory = subprocess.Popen,
) -> LiveSequencePlan:
    """Prepare an exact Reverse: 1999 plan using only prepared-story inputs."""
    story_index = _required_file(story_index, "Story index")
    try:
        story = load_story_index_document(story_index)
    except (OSError, StoryIndexError) as error:
        raise PreparedSequenceError(
            f"Unable to read prepared story: {error}"
        ) from error
    source_bundle = story.metadata.get("source_bundle")
    if not isinstance(source_bundle, str) or not source_bundle.strip():
        raise PreparedSequenceError("Prepared story has no installed source bundle.")
    if story.game != "Reverse: 1999":
        raise PreparedSequenceError("Prepared story is not for Reverse: 1999.")
    chapters = tuple(
        sorted({record.chapter for record in story.records if record.chapter})
    )
    return prepare_reverse1999_sequence(
        story_index,
        source_bundle,
        chapters,
        output,
        cancellation=cancellation,
        command=command,
        popen_factory=popen_factory,
    )


def _validated_plan(
    path: Path,
    story_index: Path,
    source_sha256: str,
    selected: tuple[str, ...],
) -> LiveSequencePlan:
    try:
        plan = load_live_sequence_plan(path, story_index)
    except (OSError, LiveSequencePlanError) as error:
        raise PreparedSequenceError(f"Invalid live-sequence plan: {error}") from error
    if plan.game_id != "reverse1999":
        raise PreparedSequenceError("Live-sequence plan is not for Reverse: 1999.")
    if plan.source_extract_sha256 != source_sha256:
        raise PreparedSequenceError(
            "Live-sequence plan belongs to different story source bytes."
        )
    if tuple(chapter.chapter for chapter in plan.chapters) != selected:
        raise PreparedSequenceError(
            "Live-sequence plan does not cover exactly the selected chapters."
        )
    for event in plan.events.values():
        if event.control == "manual":
            raise PreparedSequenceError(
                f"Live-sequence plan has a manual boundary at {event.event_id!r}."
            )
        if event.control != "terminal" and len(event.successors) != 1:
            raise PreparedSequenceError(
                f"Live-sequence plan has no unique successor at {event.event_id!r}."
            )
    return plan


def _run(
    arguments: Sequence[str],
    cancellation: Cancellation | None,
    popen_factory: PopenFactory,
) -> None:
    _raise_if_cancelled(cancellation)
    try:
        process = popen_factory(
            tuple(arguments), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except OSError as error:
        raise PreparedSequenceError(
            f"Unable to start live-sequence publisher: {error}"
        ) from error
    try:
        while True:
            if (
                cancellation is not None
                and cancellation.is_set()
                and process.poll() is None
            ):
                raise PreparedSequenceCancelled("Sequence preparation cancelled")
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            terminate_process(process)
    if process.returncode:
        detail = last_output_line(stderr) or last_output_line(stdout)
        suffix = f": {detail}" if detail else ""
        raise PreparedSequenceError(f"Live-sequence publisher failed{suffix}")


def _required_file(value: PathInput, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise PreparedSequenceError(f"{label} is not a regular file: {path}")
    return path


def _chapters(chapters: Sequence[object]) -> tuple[str, ...]:
    selected = tuple(
        sorted({str(chapter).strip() for chapter in chapters if str(chapter).strip()})
    )
    if not selected:
        raise PreparedSequenceError("At least one story chapter is required.")
    return selected


def _checksum(path: Path, label: str) -> str:
    try:
        return sha256_file(path)
    except OSError as error:
        raise PreparedSequenceError(
            f"Unable to checksum {label.casefold()}: {error}"
        ) from error


def _raise_if_cancelled(cancellation: Cancellation | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise PreparedSequenceCancelled("Sequence preparation cancelled")


__all__ = [
    "PreparedSequenceCancelled",
    "PreparedSequenceError",
    "prepare_sequence_for_prepared_story",
    "prepare_reverse1999_sequence",
    "reverse1999_live_sequence_command",
]

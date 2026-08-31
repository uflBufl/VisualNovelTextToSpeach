"""Materialize private generation inputs from a player-facing voice plan."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
from vntts_artifacts.audio import probe_pcm16_mono_wav, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import (
    StoryIndexError,
    load_story_index_document,
    write_story_index_document,
)
from vntts_artifacts.voice_manifest import VoiceManifestError, write_voice_manifest

from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
)
from vntts.authoring.queue_builder import (
    GenerationQueueBuildError,
    inspect_generation_queue,
    publish_generation_queue,
)
from vntts.pregeneration_voices import VoicePlan
from vntts.versioned_json import read_versioned_json, write_versioned_json
from vntts.voices import CharacterVoiceRegistry, read_voice_reference_bytes

generation_input_schema_version = 1


class PregenerationQueueError(RuntimeError):
    """Resolved voices cannot yet produce safe private generation inputs."""


@dataclass(frozen=True)
class PregenerationInput:
    identity: str
    directory: Path
    story_index: Path
    voice_manifest: Path
    queue: Path
    queue_sha256: str
    queue_items: int
    ready_items: int
    narrator_fallback_roles: tuple[str, ...]


class PregenerationInputStore:
    def __init__(self, job_store):
        self.job_store = job_store

    def materialize(self, job, plan, *, cancellation=None):
        if not isinstance(plan, VoicePlan) or plan.job_id != job.job_id:
            raise PregenerationQueueError(
                "Voice plan does not belong to this preparation"
            )
        if plan.story_index_sha256 != job.story_index_sha256:
            raise PregenerationQueueError("Voice plan dialogue identity changed")
        _raise_if_cancelled(cancellation)
        story = _load_story(job)
        registry = _load_source_registry(plan)
        selected = _selected_records(story, job.selected_line_ids)
        effective = _effective_voice_routes(plan, registry)
        identity = _digest(
            {
                "job_id": job.job_id,
                "story_index_sha256": job.story_index_sha256,
                "selected_line_ids": list(job.selected_line_ids),
                "groups": [
                    {
                        "group_id": group.group_id,
                        "control_sha256": group.control_sha256,
                        "route": group.route,
                        "source_id": group.source_id,
                    }
                    for group in plan.groups
                ],
            }
        )
        destination = self.job_store.path_for(job.job_id).parent / (
            f"generation-input-{identity[:16]}"
        )
        if destination.is_dir():
            return _load_existing(destination, identity)
        root = destination.parent
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".generation-input-", dir=root))
        try:
            story_path = staging / "story-index.jsonl"
            voice_path = staging / "voice-manifest.json"
            queue_path = staging / "queue.jsonl"
            write_story_index_document(
                story_path,
                story.metadata,
                [record.to_record() for record in selected],
            )
            _raise_if_cancelled(cancellation)
            voices = _write_effective_voices(staging, effective)
            write_voice_manifest(
                voice_path,
                {"version": 2, "voices": voices},
            )
            queue_plan = inspect_generation_queue(
                story_path,
                voice_path,
                unknown_action="resolve_audio",
                generated_at=job.created_at,
            )
            publish_generation_queue(queue_plan, queue_path)
            _raise_if_cancelled(cancellation)
            fields = {
                "identity": identity,
                "job_id": job.job_id,
                "source_story_index_sha256": job.story_index_sha256,
                "voice_plan_controls_sha256": plan.synthesis_controls_sha256,
                "story_index_sha256": sha256_file(story_path),
                "voice_manifest_sha256": sha256_file(voice_path),
                "queue_sha256": sha256_file(queue_path),
                "queue_items": queue_plan.summary.queue_items,
                "ready_items": queue_plan.summary.ready,
                "narrator_fallback_roles": list(effective["narrator_roles"]),
            }
            write_versioned_json(
                staging / "input.json", generation_input_schema_version, fields
            )
            try:
                rename_directory_no_replace(staging, destination)
            except AtomicPublicationError:
                if destination.is_dir():
                    return _load_existing(destination, identity)
                raise
            staging = None
            return _load_existing(destination, identity)
        except (
            AtomicPublicationError,
            GenerationQueueBuildError,
            OSError,
            StoryIndexError,
            VoiceManifestError,
            ValueError,
        ) as error:
            raise PregenerationQueueError(
                f"Unable to prepare offline generation inputs: {error}"
            ) from error
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)


def _load_story(job):
    path = Path(job.story_index).expanduser().resolve()
    try:
        before = sha256_file(path)
        if before != job.story_index_sha256:
            raise PregenerationQueueError(
                "Selected dialogue changed after preparation was planned"
            )
        story = load_story_index_document(path)
        if sha256_file(path) != before:
            raise PregenerationQueueError("Selected dialogue changed while it was read")
        return story
    except PregenerationQueueError:
        raise
    except (OSError, StoryIndexError, ValueError) as error:
        raise PregenerationQueueError(
            f"Unable to read selected dialogue: {error}"
        ) from error


def _load_source_registry(plan):
    if not plan.voice_manifest:
        raise PregenerationQueueError(
            "Choose a narrator voice before generating offline audio"
        )
    path = Path(plan.voice_manifest).expanduser().resolve()
    try:
        before = sha256_file(path)
        if before != plan.voice_manifest_sha256:
            raise PregenerationQueueError("Character voice inventory changed")
        registry = CharacterVoiceRegistry.from_file(path)
        if sha256_file(path) != before:
            raise PregenerationQueueError(
                "Character voice inventory changed while read"
            )
        return registry
    except PregenerationQueueError:
        raise
    except (OSError, VoiceManifestError, ValueError) as error:
        raise PregenerationQueueError(
            f"Unable to read character voices: {error}"
        ) from error


def _selected_records(story, selected_line_ids):
    by_id = {record.line_id: record for record in story.records}
    try:
        records = tuple(by_id[line_id] for line_id in selected_line_ids)
    except KeyError as error:
        raise PregenerationQueueError(
            "Selected dialogue changed after preparation was planned"
        ) from error
    if len(records) != len(selected_line_ids):
        raise PregenerationQueueError("Selected dialogue contains duplicate identities")
    return records


def _effective_voice_routes(plan, registry):
    routes = {}
    narrator_roles = set()
    for group in plan.groups:
        target = "Narrator" if group.route == "narrator" else group.character
        if group.route == "narrator" and group.character != "Narrator":
            narrator_roles.add(group.character)
        if not group.source_character:
            if group.route == "narrator":
                raise PregenerationQueueError(
                    "Choose a narrator voice before generating offline audio"
                )
            raise PregenerationQueueError(
                f"Choose a voice for {group.character} before generating offline audio"
            )
        voice = registry.resolve(group.source_character)
        if voice is None or not voice.references:
            raise PregenerationQueueError(
                f"The selected voice for {group.character} has no usable reference"
            )
        observed = tuple(
            hashlib.sha256(read_voice_reference_bytes(voice, path)).hexdigest()
            for path in voice.references
        )
        if observed != group.reference_sha256s:
            raise PregenerationQueueError(
                f"The selected voice reference changed for {group.character}"
            )
        previous = routes.get(target)
        choice = (voice, observed)
        if previous is not None and (
            previous[0].speaker != voice.speaker or previous[1] != observed
        ):
            raise PregenerationQueueError(
                f"Choose one voice for all {target} variants before generation"
            )
        routes[target] = choice
    return {
        "routes": routes,
        "narrator_roles": tuple(sorted(narrator_roles, key=str.casefold)),
    }


def _write_effective_voices(staging, effective):
    references = staging / "references"
    references.mkdir()
    copied = {}
    entries = []
    for character, (voice, expected_hashes) in sorted(
        effective["routes"].items(), key=lambda item: item[0].casefold()
    ):
        relative_references = []
        for source, expected in zip(voice.references, expected_hashes):
            payload = read_voice_reference_bytes(voice, source)
            if hashlib.sha256(payload).hexdigest() != expected:
                raise PregenerationQueueError(
                    f"The selected voice reference changed for {character}"
                )
            relative = copied.get(expected)
            if relative is None:
                relative = f"references/{expected}.wav"
                _write_reference_wav(staging / relative, payload, character)
                copied[expected] = relative
            relative_references.append(relative)
        entries.append(
            {
                "character": character,
                "speaker": voice.speaker,
                "aliases": [],
                "references": relative_references,
            }
        )
    return entries


def _write_reference_wav(path, payload, character):
    try:
        samples, sample_rate = sf.read(
            io.BytesIO(payload), dtype="float32", always_2d=True
        )
    except (RuntimeError, TypeError, ValueError) as error:
        raise PregenerationQueueError(
            f"Unable to decode the selected voice for {character}: {error}"
        ) from error
    if samples.size == 0 or sample_rate <= 0 or not np.isfinite(samples).all():
        raise PregenerationQueueError(
            f"The selected voice for {character} contains invalid audio"
        )
    mono = samples.mean(axis=1, dtype=np.float64)
    write_pcm16_wav(path, mono, int(sample_rate))
    probe_pcm16_mono_wav(path)


def _load_existing(directory, identity):
    try:
        document = read_versioned_json(
            directory / "input.json",
            schema_version=generation_input_schema_version,
            document_name="offline generation input",
        )
        if document.get("identity") != identity:
            raise ValueError("generation input identity changed")
        paths = {
            "story_index": directory / "story-index.jsonl",
            "voice_manifest": directory / "voice-manifest.json",
            "queue": directory / "queue.jsonl",
        }
        for name, path in paths.items():
            if sha256_file(path) != document.get(f"{name}_sha256"):
                raise ValueError(f"{name.replace('_', ' ')} changed")
        roles = document.get("narrator_fallback_roles")
        if not isinstance(roles, list) or not all(
            isinstance(value, str) and value.strip() for value in roles
        ):
            raise ValueError("narrator fallback roles are invalid")
        return PregenerationInput(
            identity=identity,
            directory=directory,
            story_index=paths["story_index"],
            voice_manifest=paths["voice_manifest"],
            queue=paths["queue"],
            queue_sha256=document["queue_sha256"],
            queue_items=_nonnegative_int(document.get("queue_items"), "queue items"),
            ready_items=_nonnegative_int(document.get("ready_items"), "ready items"),
            narrator_fallback_roles=tuple(roles),
        )
    except (OSError, TypeError, ValueError) as error:
        raise PregenerationQueueError(
            f"Saved offline generation inputs are invalid: {error}"
        ) from error


def _nonnegative_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _raise_if_cancelled(cancellation):
    if cancellation is not None and cancellation.is_set():
        raise PregenerationQueueError("Offline preparation was cancelled")


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "PregenerationInput",
    "PregenerationInputStore",
    "PregenerationQueueError",
]

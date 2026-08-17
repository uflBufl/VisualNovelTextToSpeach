"""Resumable device-independent generation from shared voice queues."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import (
    PCM16_MONO_WAV_FORMAT,
    Pcm16MonoWavError,
    probe_pcm16_mono_wav,
    read_pcm16_mono_wav,
    write_pcm16_wav,
)
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import (
    GeneratedAudioManifestError,
    write_generated_audio_manifest,
)
from vntts_artifacts.text_utils import slugify
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
)

from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisCompletion,
    SynthesisRequest,
)
from vntts.voices import synthesis_character_for_line

STATE_SCHEMA = "vntts.authoring-generation-state"
STATE_VERSION = 1
LEGACY_STATE_SCHEMA = "r1999.bulk-generation-state"
LEGACY_STATE_VERSION = 1
LEASE_SCHEMA = "vntts.authoring-generation-lease"
LEASE_VERSION = 1
NO_PROMPT_SHA256 = hashlib.sha256(b"").hexdigest()
SILENCE_DBFS = -45.0
SILENCE_FRAME_MS = 80
MAX_LEADING_SILENCE_SECONDS = 0.8
MAX_TRAILING_SILENCE_SECONDS = 0.8
MAX_INTERNAL_SILENCE_SECONDS = 1.2
MAX_SILENCE_RATIO = 0.5
PURE_SOUND_EFFECT_PATTERN = re.compile(r'^\s*["“”]?\*[^*]+\*["“”]?[.!?]?\s*$')
SHORT_TRAILING_ELLIPSIS_PATTERN = re.compile(
    r"^\s*(?P<spoken>[\w'’]+(?:\s+[\w'’]+)?)\s*(?:\.{3}|…)\s*$"
)


class BulkGenerationError(RuntimeError):
    """A queue cannot be generated or resumed safely."""


class BulkGenerationSourceChangedError(BulkGenerationError):
    """A queue or synthesis control changed during a generation snapshot."""


class BulkGenerationProvenanceError(BulkGenerationError):
    """Typed render diagnostics contradict declared synthesis provenance."""


@dataclass(frozen=True)
class AudioQuality:
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_count: int
    peak: float


@dataclass(frozen=True)
class SpeechQuality:
    silence_ratio: float
    leading_silence_seconds: float
    trailing_silence_seconds: float
    longest_internal_silence_seconds: float


@dataclass(frozen=True)
class BulkGenerationResult:
    generated: int
    failed: int
    skipped_existing: int
    skipped_actions: int
    skipped_characters: int
    skipped_items: int
    cancelled: bool
    state: Path
    manifest: Path

    def to_dict(self):
        payload = asdict(self)
        payload["state"] = str(self.state)
        payload["manifest"] = str(self.manifest)
        return payload


@dataclass(frozen=True)
class ReviewAuthority:
    """Exact immutable inputs that one human review decision applies to."""

    queue_sha256: str
    state_sha256: str
    item_sha256: str
    audio_sha256: str


def generation_review_authority(state_path, queue_id):
    """Snapshot one reviewable state item and its exact validated WAV."""
    state_path = Path(state_path).expanduser().resolve()
    state = load_generation_state(state_path)
    item = state.get("items", {}).get(queue_id)
    if not isinstance(item, dict) or item.get("status") not in {
        "generated",
        "approved",
    }:
        raise BulkGenerationError(f"Generated queue item does not exist: {queue_id}")
    relative = _safe_relative(item.get("path"), f"State item {queue_id!r} path")
    audio = _within(state_path.parent, relative, "Generated WAV")
    _validate_success_file(queue_id, item, audio)
    return ReviewAuthority(
        queue_sha256=state["queue_sha256"],
        state_sha256=sha256_file(state_path),
        item_sha256=_canonical_sha256(item),
        audio_sha256=sha256_file(audio),
    )


def _assert_review_authority(
    state_path,
    queue_id,
    expected_authority,
    queue_path,
):
    if not isinstance(expected_authority, ReviewAuthority):
        raise BulkGenerationError("Review authority snapshot is invalid")
    actual = generation_review_authority(state_path, queue_id)
    if actual != expected_authority:
        raise BulkGenerationError(
            "Review authority changed after the item was displayed; refresh before deciding"
        )
    if queue_path is None or sha256_file(queue_path) != actual.queue_sha256:
        raise BulkGenerationError(
            "Review queue changed after the item was displayed; refresh before deciding"
        )


def inspect_generated_wav(path):
    """Validate the normalized generated-audio WAV contract."""
    try:
        info = probe_pcm16_mono_wav(path)
    except (OSError, Pcm16MonoWavError) as error:
        raise BulkGenerationError(
            f"Generated output is not a readable PCM16 mono WAV: {error}"
        ) from error
    if info.sample_rate < 16_000:
        raise BulkGenerationError("Generated WAV sample rate must be at least 16 kHz")
    if info.duration_seconds < 0.1 or info.duration_seconds > 180:
        raise BulkGenerationError(
            f"Generated WAV duration is implausible: {info.duration_seconds:.2f}s"
        )
    if info.peak < 0.001:
        raise BulkGenerationError("Generated WAV is effectively silent")
    if info.peak >= 1.0:
        raise BulkGenerationError("Generated WAV is clipped")
    return AudioQuality(
        duration_seconds=round(info.duration_seconds, 4),
        sample_rate=info.sample_rate,
        channels=1,
        sample_count=info.sample_count,
        peak=round(info.peak, 6),
    )


def sha256_control_path(path):
    """Hash one immutable synthesis control file or complete directory tree."""
    try:
        path = Path(path).expanduser().resolve()
        if path.is_file():
            return sha256_file(path)
        if not path.is_dir():
            raise BulkGenerationError(f"Generation control does not exist: {path}")
        digest = hashlib.sha256()
        for candidate in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(path).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(bytes.fromhex(sha256_file(candidate)))
        return digest.hexdigest()
    except BulkGenerationError:
        raise
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to read generation control {path}: {error}"
        ) from error


def inspect_generated_speech(path):
    """Reject long silence spans that pass basic peak/duration validation."""
    try:
        samples, info = read_pcm16_mono_wav(path)
    except (OSError, Pcm16MonoWavError) as error:
        raise BulkGenerationError(
            f"Unable to analyze generated speech: {error}"
        ) from error
    samples = np.asarray(samples, dtype=np.float32)
    frame_samples = max(1, round(info.sample_rate * SILENCE_FRAME_MS / 1000))
    frame_rms = np.asarray(
        [
            np.sqrt(np.mean(samples[start : start + frame_samples] ** 2))
            for start in range(0, len(samples), frame_samples)
        ]
    )
    silent = frame_rms <= 10 ** (SILENCE_DBFS / 20.0)
    active_indices = np.flatnonzero(~silent)
    if not len(active_indices):
        quality = SpeechQuality(1.0, info.duration_seconds, info.duration_seconds, 0.0)
    else:
        first_active = int(active_indices[0])
        last_active = int(active_indices[-1])
        longest_internal = 0
        current_internal = 0
        for is_silent in silent[first_active + 1 : last_active]:
            if is_silent:
                current_internal += 1
                longest_internal = max(longest_internal, current_internal)
            else:
                current_internal = 0
        frame_seconds = frame_samples / info.sample_rate
        quality = SpeechQuality(
            silence_ratio=round(float(np.mean(silent)), 4),
            leading_silence_seconds=round(first_active * frame_seconds, 3),
            trailing_silence_seconds=round(
                (len(silent) - last_active - 1) * frame_seconds, 3
            ),
            longest_internal_silence_seconds=round(longest_internal * frame_seconds, 3),
        )
    failures = []
    if quality.leading_silence_seconds > MAX_LEADING_SILENCE_SECONDS:
        failures.append(f"{quality.leading_silence_seconds:.2f}s leading silence")
    if quality.trailing_silence_seconds > MAX_TRAILING_SILENCE_SECONDS:
        failures.append(f"{quality.trailing_silence_seconds:.2f}s trailing silence")
    if quality.longest_internal_silence_seconds > MAX_INTERNAL_SILENCE_SECONDS:
        failures.append(
            f"{quality.longest_internal_silence_seconds:.2f}s internal silence"
        )
    if quality.silence_ratio > MAX_SILENCE_RATIO:
        failures.append(f"{quality.silence_ratio:.0%} silent frames")
    if failures:
        raise BulkGenerationError(
            "Generated WAV failed speech-silence validation: " + ", ".join(failures)
        )
    return quality


def is_spoken_queue_item(item):
    """Skip explicit legacy pure-SFX records without game-specific IDs."""
    document = item.document if hasattr(item, "document") else item
    if document.get("speakable") is False:
        return False
    return PURE_SOUND_EFFECT_PATTERN.fullmatch(str(document.get("text") or "")) is None


def normalize_short_trailing_ellipsis(text):
    """Give one/two-word ellipses an audible terminal boundary for MOSS."""
    match = SHORT_TRAILING_ELLIPSIS_PATTERN.fullmatch(str(text or ""))
    return str(text) if match is None else match.group("spoken") + "."


def load_generation_state(state_path, queue_path=None):
    """Load either VNTTS-owned or preserved legacy state and verify its files."""
    state_path = Path(state_path).expanduser().resolve()
    state = _load_json(state_path, "generation state")
    queue = None
    queue_sha256 = state.get("queue_sha256")
    if queue_path is not None:
        queue_path = Path(queue_path).expanduser().resolve()
        try:
            queue = VoiceGenerationQueue.load(queue_path)
        except VoiceGenerationQueueError as error:
            raise BulkGenerationError(str(error)) from error
        queue_sha256 = sha256_file(queue_path)
    _validate_state_document(state, state_path.parent, queue, queue_sha256)
    return state


def run_bulk_generation(
    queue_path,
    output_directory,
    backend,
    *,
    provider,
    model,
    generation_profile="stable",
    limit=None,
    retries=2,
    include_prefer_source=False,
    include_characters=None,
    include_queue_ids=None,
    item_filter=None,
    seed=0,
    cancellation=None,
    control_files=None,
    text_transform=None,
    text_transform_id=None,
    process_checker=None,
    workspace_output_identity=None,
):
    """Render selected queue items with no device playback and resumable state."""
    limit = _nonnegative_optional_int(limit, "Generation limit")
    retries = _nonnegative_int(retries, "Retry count")
    seed = _integer(seed, "Base seed")
    provider = _required_text(provider, "Provider")
    model = _required_text(model, "Model")
    generation_profile = _required_text(generation_profile, "Generation profile")
    if text_transform is not None and not callable(text_transform):
        raise BulkGenerationError("Text transform must be callable")
    if text_transform is not None:
        text_transform_id = _required_text(text_transform_id, "Text transform identity")
    elif text_transform_id is not None:
        raise BulkGenerationError("Text transform identity requires a text transform")
    render = getattr(backend, "render", None)
    if not callable(render):
        raise BulkGenerationError(
            "Generation backend must implement render(SynthesisRequest)"
        )
    backend_name = _required_text(
        getattr(backend, "name", provider), "Backend identity"
    )
    if backend_name != provider:
        raise BulkGenerationError(
            f"Configured provider {provider!r} does not match backend {backend_name!r}"
        )
    backend_model = getattr(backend, "model_identity", None) or getattr(
        backend, "model_name", None
    )
    if backend_model is None:
        backend_model = backend_name
    if str(backend_model) != model:
        raise BulkGenerationError(
            f"Configured model {model!r} does not match backend model {backend_model!r}"
        )

    queue_path = Path(queue_path).expanduser().resolve()
    output_argument = Path(output_directory).expanduser()
    if workspace_output_identity is not None:
        _assert_workspace_output_identity(output_argument, workspace_output_identity)
    output_directory = output_argument.resolve()
    queue, queue_sha256 = _load_stable_queue(queue_path)
    selected_queue_ids = None
    if include_queue_ids is not None:
        selected_queue_ids = {
            _required_text(value, "Selected queue ID") for value in include_queue_ids
        }
        known_queue_ids = {item.queue_id for item in queue.items}
        unknown_queue_ids = selected_queue_ids - known_queue_ids
        if unknown_queue_ids:
            raise BulkGenerationError(
                "Selected queue IDs are absent from the bound queue: "
                + ", ".join(sorted(unknown_queue_ids))
            )
    controls = _snapshot_control_files(control_files or {})
    control_records = [_stored_control(value) for value in controls]
    provenance_sha256 = _canonical_sha256(
        {
            "provider": provider,
            "model": model,
            "generation_profile": generation_profile,
            "text_transform": text_transform_id,
            "controls": [
                {"role": value["role"], "sha256": value["sha256"]} for value in controls
            ],
        }
    )
    state_path = output_directory / "generation-state.json"
    manifest_path = output_directory / "manifest.json"
    output_directory.mkdir(parents=True, exist_ok=True)

    with _GenerationLease(
        output_directory,
        queue_sha256,
        process_checker=process_checker or process_is_alive,
    ) as lease:
        if workspace_output_identity is not None:
            _assert_workspace_output_identity(
                output_argument, workspace_output_identity
            )
        interrupted_job = _guard_job_process(
            output_directory, process_checker or process_is_alive
        )
        state = _load_or_create_state(state_path, output_directory, queue, queue_sha256)
        if state["schema"] == STATE_SCHEMA:
            registry = state.setdefault("synthesis_controls", {})
            existing_controls = registry.get(provenance_sha256)
            if existing_controls is not None and existing_controls != control_records:
                raise BulkGenerationProvenanceError(
                    "Stored synthesis controls conflict with this run"
                )
            if existing_controls is None:
                registry[provenance_sha256] = control_records
                atomic_write_json(state_path, state, sort_keys=True)
        if interrupted_job is not None:
            interrupted_processes = state.setdefault("interrupted_processes", [])
            if not any(
                isinstance(value, dict)
                and value.get("job_sha256") == interrupted_job["job_sha256"]
                for value in interrupted_processes
            ):
                interrupted_processes.append(interrupted_job)
                atomic_write_json(state_path, state, sort_keys=True)
        _reconcile_interrupted_attempt(state_path, state, queue)
        state.setdefault("game", queue.metadata.get("game"))
        state.setdefault("language", queue.metadata.get("language"))

        eligible_actions = {"generate"}
        if include_prefer_source:
            eligible_actions.add("prefer_source_audio")
        candidates = [item for item in queue.items if item.action in eligible_actions]
        skipped_actions = len(queue.items) - len(candidates)
        character_filter = (
            None if include_characters is None else set(include_characters)
        )
        skipped_characters = 0
        if character_filter is not None:
            filtered = [
                item
                for item in candidates
                if synthesis_character_for_line(item.speaker, item.voice_character)
                in character_filter
            ]
            skipped_characters = len(candidates) - len(filtered)
            candidates = filtered
        skipped_items = 0
        if selected_queue_ids is not None:
            filtered = [
                item for item in candidates if item.queue_id in selected_queue_ids
            ]
            skipped_items += len(candidates) - len(filtered)
            candidates = filtered
        if item_filter is not None:
            filtered = [item for item in candidates if item_filter(item)]
            skipped_items += len(candidates) - len(filtered)
            candidates = filtered
        if limit is not None:
            candidates = candidates[:limit]

        generated = 0
        skipped_existing = 0
        cancelled = False
        for item in candidates:
            queue_id = item.queue_id
            existing = state["items"].get(queue_id, {})
            if existing.get("status") in {"generated", "approved"}:
                _validate_success_item(
                    queue_id,
                    existing,
                    output_directory,
                    item,
                    state_schema=state["schema"],
                )
                skipped_existing += 1
                continue

            voice = _required_text(
                synthesis_character_for_line(item.speaker, item.voice_character),
                f"Queue item {queue_id!r} voice",
            )
            queue_annotations_sha256 = _canonical_sha256(
                item.document.get("prompt_adapters") or {}
            )
            prompt_sha256 = NO_PROMPT_SHA256
            synthesis_text = (
                item.text if text_transform is None else text_transform(item.text)
            )
            if not isinstance(synthesis_text, str) or not synthesis_text.strip():
                raise BulkGenerationError(
                    f"Text transform returned no speech for queue item {queue_id!r}"
                )
            synthesis_text_sha256 = hashlib.sha256(
                synthesis_text.encode("utf-8")
            ).hexdigest()
            relative = _audio_relative_path(voice, queue_id)
            if workspace_output_identity is not None:
                _assert_workspace_output_identity(
                    output_argument, workspace_output_identity
                )
            destination = _within(output_directory, relative, "Generated WAV")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                _archive_interrupted_artifact(output_directory, destination)
            attempts = _nonnegative_int(existing.get("attempts", 0), "Attempts")
            run_attempts = 0
            last_error = str(existing.get("last_error") or "") or None
            while run_attempts <= retries:
                attempts += 1
                run_attempts += 1
                attempt_seed = seed + attempts - 1
                started_at = _now()
                partial = destination.with_suffix(".partial.wav")
                if partial.exists():
                    _archive_interrupted_artifact(output_directory, partial)
                _write_active(
                    state_path,
                    state,
                    item,
                    provider=provider,
                    model=model,
                    generation_profile=generation_profile,
                    prompt_sha256=prompt_sha256,
                    queue_annotations_sha256=queue_annotations_sha256,
                    synthesis_text_sha256=synthesis_text_sha256,
                    text_transform_id=text_transform_id,
                    synthesis_provenance_sha256=provenance_sha256,
                    phase="generating",
                    attempt=run_attempts,
                    attempt_limit=retries + 1,
                    total_attempts=attempts,
                    seed=attempt_seed,
                    started_at=started_at,
                    last_error=last_error,
                )
                request = SynthesisRequest(
                    voice=voice,
                    text=synthesis_text,
                    seed=attempt_seed,
                    generation_profile=generation_profile,
                    cancellation=cancellation,
                    cache_policy=SynthesisCachePolicy.BYPASS,
                )
                try:
                    rendered = render(request).collect()
                    _validate_render_result(rendered, request, provider)
                    _assert_control_files_unchanged(controls)
                    if workspace_output_identity is not None:
                        _assert_workspace_output_identity(
                            output_argument, workspace_output_identity
                        )
                    lease.assert_owned()
                    _write_active_phase(state_path, state, "validating")
                    write_pcm16_wav(
                        partial,
                        _generated_mono_pcm(rendered.pcm),
                        rendered.sample_rate,
                    )
                    quality = inspect_generated_wav(partial)
                    speech_quality = inspect_generated_speech(partial)
                    file_sha256 = sha256_file(partial)
                    _write_active_phase(state_path, state, "publishing")
                    if workspace_output_identity is not None:
                        _assert_workspace_output_identity(
                            output_argument, workspace_output_identity
                        )
                    os.replace(partial, destination)
                    state["items"][queue_id] = {
                        "status": "generated",
                        "review_status": "pending_review",
                        "attempts": attempts,
                        "path": relative.as_posix(),
                        "line_id": item.line_id,
                        "text_sha256": item.text_sha256,
                        "file_sha256": file_sha256,
                        "provider": provider,
                        "model": model,
                        "prompt_sha256": prompt_sha256,
                        "prompt_applied": False,
                        "queue_annotations_sha256": queue_annotations_sha256,
                        "synthesis_text_sha256": synthesis_text_sha256,
                        "text_transform": text_transform_id,
                        "synthesis_provenance_sha256": provenance_sha256,
                        "seed": attempt_seed,
                        "generation_profile": generation_profile,
                        "voice_character": voice,
                        "quality": asdict(quality),
                        "speech_quality": asdict(speech_quality),
                        "updated_at": _now(),
                    }
                    state["active"] = None
                    atomic_write_json(state_path, state, sort_keys=True)
                    generated += 1
                    break
                except (
                    BulkGenerationSourceChangedError,
                    BulkGenerationProvenanceError,
                ):
                    if partial.exists():
                        partial.unlink()
                    raise
                except Exception as error:
                    if partial.exists():
                        partial.unlink()
                    completion = (
                        getattr(rendered, "completion", None)
                        if "rendered" in locals()
                        else None
                    )
                    last_error = str(error) or error.__class__.__name__
                    state["items"][queue_id] = {
                        "status": "failed",
                        "attempts": attempts,
                        "seed": attempt_seed,
                        "last_error": last_error,
                        "provider": provider,
                        "model": model,
                        "generation_profile": generation_profile,
                        "prompt_sha256": prompt_sha256,
                        "prompt_applied": False,
                        "queue_annotations_sha256": queue_annotations_sha256,
                        "synthesis_text_sha256": synthesis_text_sha256,
                        "text_transform": text_transform_id,
                        "synthesis_provenance_sha256": provenance_sha256,
                        "updated_at": _now(),
                    }
                    is_cancelled = (
                        completion is SynthesisCompletion.CANCELLED
                        or request.cancellation_requested()
                    )
                    if run_attempts <= retries and not is_cancelled:
                        _write_active_phase(
                            state_path, state, "retrying", last_error=last_error
                        )
                    else:
                        state["active"] = None
                        atomic_write_json(state_path, state, sort_keys=True)
                    if is_cancelled:
                        cancelled = True
                        break
                    if run_attempts > retries:
                        break
                finally:
                    if "rendered" in locals():
                        del rendered
            if cancelled:
                break

        _assert_sources_unchanged(queue_path, queue_sha256, controls)
        if workspace_output_identity is not None:
            _assert_workspace_output_identity(
                output_argument, workspace_output_identity
            )
        lease.assert_owned()
        publish_generated_manifest(
            state_path, manifest_path=manifest_path, _lease_held=True
        )
        failed = sum(
            value.get("status") == "failed" for value in state["items"].values()
        )
        return BulkGenerationResult(
            generated=generated,
            failed=failed,
            skipped_existing=skipped_existing,
            skipped_actions=skipped_actions,
            skipped_characters=skipped_characters,
            skipped_items=skipped_items,
            cancelled=cancelled,
            state=state_path,
            manifest=manifest_path,
        )


def publish_generated_manifest(state_path, *, manifest_path=None, _lease_held=False):
    """Rebuild the approved-only manifest from authoritative state."""
    state_path = Path(state_path).expanduser().resolve()
    output_directory = state_path.parent
    state = load_generation_state(state_path)
    if not _lease_held:
        with _GenerationLease(
            output_directory,
            state["queue_sha256"],
            process_checker=process_is_alive,
        ):
            return publish_generated_manifest(
                state_path, manifest_path=manifest_path, _lease_held=True
            )
    manifest_path = Path(manifest_path or output_directory / "manifest.json").resolve()
    if manifest_path.parent != output_directory:
        raise BulkGenerationError(
            "Generated-audio manifest must stay in the state directory"
        )
    _write_generated_manifest_from_state(state, output_directory, manifest_path)
    return manifest_path


def _write_generated_manifest_from_state(
    state,
    output_directory,
    manifest_path,
    *,
    entries=None,
):
    entries = (
        _approved_manifest_entries(state, output_directory)
        if entries is None
        else entries
    )
    try:
        write_generated_audio_manifest(
            manifest_path,
            {
                "game": state.get("game"),
                "language": state.get("language"),
                "source_queue_sha256": state["queue_sha256"],
                "generated_at": _now(),
            },
            entries,
        )
    except GeneratedAudioManifestError as error:
        raise BulkGenerationError(str(error)) from error


def _approved_manifest_entries(state, output_directory):
    entries = []
    for queue_id, result in state["items"].items():
        if (
            result.get("status") != "approved"
            or result.get("review_status") != "approved"
        ):
            continue
        relative = _safe_relative(result.get("path"), f"State item {queue_id!r} path")
        audio = _within(output_directory, relative, "Generated WAV")
        quality = _validate_success_file(queue_id, result, audio)
        entry = {
            "queue_id": queue_id,
            "line_id": result["line_id"],
            "text_sha256": result["text_sha256"],
            "audio": relative.as_posix(),
            "audio_format": PCM16_MONO_WAV_FORMAT,
            "audio_sha256": result["file_sha256"],
            "sample_rate": quality.sample_rate,
            "sample_count": quality.sample_count,
            "provider": result["provider"],
            "model": result["model"],
            "prompt_sha256": result["prompt_sha256"],
            "seed": result["seed"],
            "review_status": "approved",
        }
        for field in (
            "generation_profile",
            "prompt_applied",
            "queue_annotations_sha256",
            "synthesis_provenance_sha256",
            "synthesis_text_sha256",
            "text_transform",
            "voice_character",
            "speech_quality",
        ):
            if field in result:
                entry[field] = result[field]
        entries.append(entry)
    entries.sort(key=lambda entry: (entry["line_id"], entry["text_sha256"]))
    return entries


def review_generation_item(
    state_path,
    queue_id,
    decision,
    *,
    expected_authority=None,
    queue_path=None,
):
    """Persist approval/rejection, then rebuild the derived manifest."""
    if decision not in {"approved", "rejected"}:
        raise BulkGenerationError("Review decision must be approved or rejected")
    state_path = Path(state_path).expanduser().resolve()
    initial = load_generation_state(state_path)
    with _GenerationLease(
        state_path.parent,
        initial["queue_sha256"],
        process_checker=process_is_alive,
    ) as lease:
        return _review_generation_item_locked(
            state_path,
            queue_id,
            decision,
            expected_authority=expected_authority,
            queue_path=queue_path,
            lease=lease,
        )


def _review_generation_item_locked(
    state_path,
    queue_id,
    decision,
    *,
    expected_authority=None,
    queue_path=None,
    lease=None,
):
    state = load_generation_state(state_path)
    item = state.get("items", {}).get(queue_id)
    if not isinstance(item, dict) or item.get("status") not in {
        "generated",
        "approved",
    }:
        raise BulkGenerationError(f"Generated queue item does not exist: {queue_id}")
    relative = _safe_relative(item.get("path"), f"State item {queue_id!r} path")
    audio = _within(state_path.parent, relative, "Generated WAV")
    _validate_success_file(queue_id, item, audio)
    if expected_authority is not None:
        _assert_review_authority(
            state_path,
            queue_id,
            expected_authority,
            queue_path,
        )
    if lease is not None:
        lease.assert_owned()
    proposed = copy.deepcopy(state)
    proposed_item = proposed["items"][queue_id]
    proposed_item["review_status"] = decision
    proposed_item["status"] = "approved" if decision == "approved" else "generated"
    proposed_item["updated_at"] = _now()
    manifest_path = state_path.parent / "manifest.json"
    entries = _approved_manifest_entries(proposed, state_path.parent)
    transaction_id = secrets.token_hex(16)
    staged_state = state_path.with_name(f".{state_path.name}.{transaction_id}.tmp")
    staged_manifest = manifest_path.with_name(
        f".{manifest_path.name}.{transaction_id}.tmp"
    )
    try:
        atomic_write_json(staged_state, proposed, sort_keys=True)
        _write_generated_manifest_from_state(
            proposed,
            state_path.parent,
            staged_manifest,
            entries=entries,
        )
        if expected_authority is not None:
            _assert_review_authority(
                state_path,
                queue_id,
                expected_authority,
                queue_path,
            )
        if lease is not None:
            lease.assert_owned()
        try:
            os.replace(staged_state, state_path)
        except OSError as error:
            raise BulkGenerationError(
                f"Unable to save review decision: {error}"
            ) from error
        try:
            if lease is not None:
                try:
                    lease.assert_owned()
                except BulkGenerationError as error:
                    raise BulkGenerationError(
                        "Review decision was saved, but manifest rebuild was blocked: "
                        f"{error}"
                    ) from error
            os.replace(staged_manifest, manifest_path)
        except OSError as error:
            raise BulkGenerationError(
                f"Review decision was saved, but manifest rebuild failed: {error}"
            ) from error
    finally:
        for staged in (staged_state, staged_manifest):
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    if lease is not None:
        lease.mark_committed()
    return proposed


def process_is_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_started_at(pid):
    """Return the operating-system process start identity when inspectable."""
    try:
        pid = int(pid)
        completed = subprocess.run(
            ("ps", "-o", "lstart=", "-p", str(pid)),
            check=True,
            capture_output=True,
            text=True,
        )
    except (TypeError, ValueError, OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


# Private compatibility for the existing publication lease until its next schema bump.
_process_started_at = process_started_at


class _GenerationLease:
    def __init__(self, output_directory, queue_sha256, *, process_checker):
        self.output_directory = Path(output_directory)
        self.path = self.output_directory / ".generation-lease.json"
        self.queue_sha256 = queue_sha256
        self.process_checker = process_checker
        self.lease_id = secrets.token_hex(16)
        self.committed = False
        self.document = None

    def __enter__(self):
        if self.path.exists():
            lease = _load_json(self.path, "generation lease")
            if (
                lease.get("schema") != LEASE_SCHEMA
                or lease.get("schema_version") != LEASE_VERSION
                or not isinstance(lease.get("queue_sha256"), str)
            ):
                raise BulkGenerationError(
                    f"Unrecognized generation lease blocks output: {self.path}"
                )
            same_host = lease.get("hostname") in {None, socket.gethostname()}
            live = same_host and self.process_checker(lease.get("pid"))
            recorded_start = lease.get("process_started_at")
            if live and recorded_start is not None:
                actual_start = process_started_at(lease.get("pid"))
                if actual_start is not None:
                    live = recorded_start == actual_start
            if not same_host or live:
                raise BulkGenerationError(
                    f"Another generation process is active with PID {lease.get('pid')}"
                )
            _archive_interrupted_artifact(self.output_directory, self.path)
        lease = {
            "schema": LEASE_SCHEMA,
            "schema_version": LEASE_VERSION,
            "queue_sha256": self.queue_sha256,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "process_started_at": process_started_at(os.getpid()),
            "lease_id": self.lease_id,
            "started_at": _now(),
        }
        self.document = lease
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError as error:
            raise BulkGenerationError(
                "Another generation process acquired the output"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(lease, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        return self

    def __exit__(self, _error_type, _error, _traceback):
        ownership_error = None
        try:
            current = _load_json(self.path, "generation lease")
        except BulkGenerationError as error:
            ownership_error = error
        else:
            if current == self.document:
                self.path.unlink()
            else:
                ownership_error = BulkGenerationError(
                    "Generation lease ownership changed during the run"
                )
        if ownership_error is not None and _error_type is None and not self.committed:
            raise ownership_error

    def assert_owned(self):
        current = _load_json(self.path, "generation lease")
        if current != self.document:
            raise BulkGenerationError(
                "Generation lease ownership changed during the run"
            )

    def mark_committed(self):
        """Do not report cleanup ambiguity as failure after an external commit."""
        self.committed = True


def _load_or_create_state(state_path, output_directory, queue, queue_sha256):
    if state_path.is_file():
        state = _load_json(state_path, "generation state")
        _validate_state_document(state, output_directory, queue, queue_sha256)
        return state
    state = {
        "schema": STATE_SCHEMA,
        "schema_version": STATE_VERSION,
        "queue_sha256": queue_sha256,
        "game": queue.metadata.get("game"),
        "language": queue.metadata.get("language"),
        "items": {},
        "active": None,
        "synthesis_controls": {},
    }
    atomic_write_json(state_path, state, sort_keys=True)
    return state


def _validate_state_document(state, output_directory, queue, queue_sha256):
    schema_pair = (state.get("schema"), state.get("schema_version"))
    if schema_pair not in {
        (STATE_SCHEMA, STATE_VERSION),
        (LEGACY_STATE_SCHEMA, LEGACY_STATE_VERSION),
    }:
        raise BulkGenerationError(
            f"Unsupported generation state schema: {schema_pair!r}"
        )
    if queue_sha256 is not None and state.get("queue_sha256") != queue_sha256:
        raise BulkGenerationError(
            "Generation queue changed; use a new output directory"
        )
    _required_sha256(state.get("queue_sha256"), "Generation state queue_sha256")
    for field in ("game", "language"):
        value = state.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise BulkGenerationError(
                f"Generation state {field} must be non-empty text or null"
            )
        if queue is None:
            continue
        expected = queue.metadata.get(field)
        if state["schema"] == LEGACY_STATE_SCHEMA and value is None:
            continue
        if value != expected:
            raise BulkGenerationError(
                f"Generation state {field} does not match the queue metadata"
            )
    if not isinstance(state.get("items"), dict):
        raise BulkGenerationError("Generation state items must be an object")
    _validate_synthesis_controls(state)
    queue_by_id = (
        None if queue is None else {item.queue_id: item for item in queue.items}
    )
    for queue_id, result in state["items"].items():
        if queue_by_id is not None and queue_id not in queue_by_id:
            raise BulkGenerationError(
                f"Generation state references unknown queue_id {queue_id!r}"
            )
        if not isinstance(result, dict):
            raise BulkGenerationError(f"Generation state item {queue_id!r} is invalid")
        status = result.get("status")
        review = result.get("review_status")
        valid = {
            "failed": {None},
            "generated": {"pending_review", "rejected"},
            "approved": {"approved"},
        }
        if status not in valid or review not in valid[status]:
            raise BulkGenerationError(
                f"Generation state item {queue_id!r} has invalid {status!r}/{review!r} status"
            )
        _nonnegative_int(result.get("attempts", 0), f"Item {queue_id!r} attempts")
        if status == "failed":
            continue
        queue_item = None if queue_by_id is None else queue_by_id[queue_id]
        _validate_success_item(
            queue_id,
            result,
            output_directory,
            queue_item,
            state_schema=state["schema"],
        )
    active = state.get("active")
    if active is not None and not isinstance(active, dict):
        raise BulkGenerationError(
            "Generation state active attempt must be an object or null"
        )
    if isinstance(active, dict):
        _validate_active_attempt(active, queue_by_id)


def _validate_active_attempt(active, queue_by_id):
    queue_id = active.get("queue_id")
    if not isinstance(queue_id, str) or not queue_id:
        raise BulkGenerationError("Active attempt queue_id must be non-empty text")
    queue_item = None
    if queue_by_id is not None:
        queue_item = queue_by_id.get(queue_id)
        if queue_item is None:
            raise BulkGenerationError("Active attempt references an unknown queue item")
        expected = {
            "line_id": queue_item.line_id,
            "text": queue_item.text,
            "text_sha256": queue_item.text_sha256,
            "speaker": queue_item.speaker,
            "voice_character": queue_item.voice_character,
        }
        for field, value in expected.items():
            if field in active and active[field] != value:
                raise BulkGenerationError(
                    f"Active attempt {field} does not match queue item {queue_id!r}"
                )
    phase = active.get("phase")
    if phase is not None and phase not in {
        "generating",
        "validating",
        "publishing",
        "retrying",
    }:
        raise BulkGenerationError(f"Active attempt phase is invalid: {phase!r}")
    integers = {}
    for field in ("attempt", "attempt_limit", "total_attempts"):
        if field in active:
            integers[field] = _nonnegative_int(active[field], f"Active attempt {field}")
            if integers[field] < 1:
                raise BulkGenerationError(f"Active attempt {field} must be positive")
    if (
        "attempt" in integers
        and "attempt_limit" in integers
        and integers["attempt"] > integers["attempt_limit"]
    ):
        raise BulkGenerationError("Active attempt exceeds its attempt limit")
    if (
        "attempt" in integers
        and "total_attempts" in integers
        and integers["total_attempts"] < integers["attempt"]
    ):
        raise BulkGenerationError("Active cumulative attempts are inconsistent")
    if "seed" in active:
        _integer(active["seed"], "Active attempt seed")
    for field in ("started_at", "updated_at"):
        if field in active and (
            not isinstance(active[field], str) or not active[field].strip()
        ):
            raise BulkGenerationError(f"Active attempt {field} must be timestamp text")
    if active.get("last_error") is not None and not isinstance(
        active.get("last_error"), str
    ):
        raise BulkGenerationError("Active attempt last_error must be text or null")


def _validate_synthesis_controls(state):
    registry = state.get("synthesis_controls")
    if registry is None:
        return
    if not isinstance(registry, dict):
        raise BulkGenerationError(
            "Generation state synthesis_controls must be an object"
        )
    for provenance, controls in registry.items():
        _required_sha256(provenance, "Synthesis-control provenance key")
        if not isinstance(controls, list):
            raise BulkGenerationError("Synthesis-control set must be a list")
        roles = set()
        for control in controls:
            if not isinstance(control, dict):
                raise BulkGenerationError("Synthesis control must be an object")
            role = _required_text(control.get("role"), "Synthesis control role")
            if role in roles:
                raise BulkGenerationError(
                    f"Synthesis-control role is duplicated: {role!r}"
                )
            roles.add(role)
            kind = control.get("kind")
            expected_fields = {"role", "kind", "path", "sha256"}
            if kind == "directory":
                expected_fields.add("files")
            elif kind != "file":
                raise BulkGenerationError(
                    f"Synthesis control kind is invalid: {kind!r}"
                )
            if set(control) != expected_fields:
                raise BulkGenerationError(
                    f"Synthesis control {role!r} has unsupported fields"
                )
            _required_text(control.get("path"), f"Synthesis control {role!r} path")
            _required_sha256(
                control.get("sha256"), f"Synthesis control {role!r} sha256"
            )
            if kind == "directory":
                files = control.get("files")
                if not isinstance(files, list):
                    raise BulkGenerationError(
                        f"Synthesis control {role!r} files must be a list"
                    )
                parsed_files = []
                seen_paths = set()
                for file_record in files:
                    if not isinstance(file_record, dict) or set(file_record) != {
                        "path",
                        "sha256",
                    }:
                        raise BulkGenerationError(
                            f"Synthesis control {role!r} file record is invalid"
                        )
                    relative = file_record.get("path")
                    if not isinstance(relative, str) or "\\" in relative:
                        raise BulkGenerationError(
                            f"Synthesis control {role!r} file path is invalid"
                        )
                    pure = PurePosixPath(relative)
                    if pure.is_absolute() or any(
                        part in {"", ".", ".."} for part in pure.parts
                    ):
                        raise BulkGenerationError(
                            f"Synthesis control {role!r} file path is unsafe"
                        )
                    if relative in seen_paths:
                        raise BulkGenerationError(
                            f"Synthesis control {role!r} file path is duplicated"
                        )
                    seen_paths.add(relative)
                    digest = _required_sha256(
                        file_record.get("sha256"),
                        f"Synthesis control {role!r} file sha256",
                    )
                    parsed_files.append({"path": relative, "sha256": digest})
                if _control_directory_digest(parsed_files) != control["sha256"]:
                    raise BulkGenerationError(
                        f"Synthesis control {role!r} directory digest is inconsistent"
                    )


def _validate_success_item(
    queue_id, result, output_directory, queue_item, *, state_schema
):
    if queue_item is not None and (
        result.get("line_id") != queue_item.line_id
        or result.get("text_sha256") != queue_item.text_sha256
    ):
        raise BulkGenerationError(
            f"Generation state identity does not match queue item {queue_id!r}"
        )
    _required_text(result.get("provider"), f"State item {queue_id!r} provider")
    _required_text(result.get("model"), f"State item {queue_id!r} model")
    _required_sha256(
        result.get("prompt_sha256"), f"State item {queue_id!r} prompt_sha256"
    )
    _integer(result.get("seed"), f"State item {queue_id!r} seed")
    if state_schema == STATE_SCHEMA:
        _required_text(
            result.get("generation_profile"),
            f"State item {queue_id!r} generation_profile",
        )
        _required_text(
            result.get("voice_character"),
            f"State item {queue_id!r} voice_character",
        )
        _required_sha256(
            result.get("synthesis_provenance_sha256"),
            f"State item {queue_id!r} synthesis_provenance_sha256",
        )
        _required_sha256(
            result.get("queue_annotations_sha256"),
            f"State item {queue_id!r} queue_annotations_sha256",
        )
        _required_sha256(
            result.get("synthesis_text_sha256"),
            f"State item {queue_id!r} synthesis_text_sha256",
        )
        if result.get("text_transform") is not None:
            _required_text(
                result.get("text_transform"),
                f"State item {queue_id!r} text_transform",
            )
        if result.get("prompt_applied") is not False:
            raise BulkGenerationError(
                f"State item {queue_id!r} prompt_applied must be false"
            )
    relative = _safe_relative(result.get("path"), f"State item {queue_id!r} path")
    audio = _within(output_directory, relative, "Generated WAV")
    _validate_success_file(queue_id, result, audio)
    if state_schema == STATE_SCHEMA:
        actual_speech_quality = asdict(inspect_generated_speech(audio))
        if result.get("speech_quality") != actual_speech_quality:
            raise BulkGenerationError(
                f"Generated WAV speech quality mismatch for {queue_id!r}"
            )


def _validate_success_file(queue_id, result, audio):
    if not audio.is_file():
        raise BulkGenerationError(f"Generated WAV is missing for {queue_id!r}: {audio}")
    if sha256_file(audio) != result.get("file_sha256"):
        raise BulkGenerationError(f"Generated WAV checksum mismatch for {queue_id!r}")
    quality = inspect_generated_wav(audio)
    stored = result.get("quality")
    if not isinstance(stored, dict):
        raise BulkGenerationError(f"Generated WAV quality is missing for {queue_id!r}")
    expected = asdict(quality)
    for field, value in expected.items():
        if stored.get(field) != value:
            raise BulkGenerationError(
                f"Generated WAV quality {field} mismatch for {queue_id!r}"
            )
    return quality


def _reconcile_interrupted_attempt(state_path, state, queue):
    active = state.get("active")
    if not isinstance(active, dict):
        return
    queue_id = active.get("queue_id")
    if queue_id not in {item.queue_id for item in queue.items}:
        raise BulkGenerationError(
            "Interrupted attempt references an unknown queue item"
        )
    interrupted = dict(active)
    interrupted["detected_at"] = _now()
    state.setdefault("interrupted_attempts", []).append(interrupted)
    existing = state["items"].get(queue_id, {})
    if existing.get("status") not in {"generated", "approved"}:
        attempts = max(
            _nonnegative_int(existing.get("attempts", 0), "Attempts"),
            _nonnegative_int(active.get("total_attempts", 0), "Active attempts"),
        )
        state["items"][queue_id] = {
            "status": "failed",
            "attempts": attempts,
            "seed": active.get("seed"),
            "last_error": "Interrupted generation attempt",
            "updated_at": _now(),
        }
    state["active"] = None
    atomic_write_json(state_path, state, sort_keys=True)


def _write_active(
    state_path,
    state,
    item,
    *,
    provider,
    model,
    generation_profile,
    prompt_sha256,
    queue_annotations_sha256,
    synthesis_text_sha256,
    text_transform_id,
    synthesis_provenance_sha256,
    phase,
    attempt,
    attempt_limit,
    total_attempts,
    seed,
    started_at,
    last_error,
):
    state["active"] = {
        "queue_id": item.queue_id,
        "line_id": item.line_id,
        "text_sha256": item.text_sha256,
        "speaker": item.speaker,
        "voice_character": item.voice_character,
        "text": item.text,
        "phase": phase,
        "attempt": attempt,
        "attempt_limit": attempt_limit,
        "total_attempts": total_attempts,
        "seed": seed,
        "provider": provider,
        "model": model,
        "generation_profile": generation_profile,
        "prompt_sha256": prompt_sha256,
        "prompt_applied": False,
        "queue_annotations_sha256": queue_annotations_sha256,
        "synthesis_text_sha256": synthesis_text_sha256,
        "text_transform": text_transform_id,
        "synthesis_provenance_sha256": synthesis_provenance_sha256,
        "started_at": started_at,
        "updated_at": _now(),
        "last_error": last_error,
    }
    atomic_write_json(state_path, state, sort_keys=True)


def _write_active_phase(state_path, state, phase, *, last_error=None):
    active = state.get("active")
    if not isinstance(active, dict):
        raise BulkGenerationError("Generation active attempt was lost")
    active["phase"] = phase
    active["updated_at"] = _now()
    if last_error is not None:
        active["last_error"] = last_error
    atomic_write_json(state_path, state, sort_keys=True)


def _validate_render_result(result, request, provider):
    if result.completion is not SynthesisCompletion.COMPLETE:
        raise BulkGenerationError(
            "Typed render completed as "
            f"{result.completion.value} "
            f"(sample_count={result.diagnostics.sample_count}, "
            f"chunk_count={result.diagnostics.chunk_count}, "
            f"max_audio_seconds={result.limits.max_audio_seconds}, "
            f"max_tokens={result.limits.max_tokens}); WAV was not published"
        )
    if not isinstance(result.sample_rate, int) or result.sample_rate <= 0:
        raise BulkGenerationError("Typed render returned an invalid sample rate")
    if (
        result.diagnostics.seed != request.seed
        or result.diagnostics.generation_profile != request.generation_profile
    ):
        raise BulkGenerationProvenanceError(
            "Typed render diagnostics do not match the request"
        )
    if result.diagnostics.backend != provider:
        raise BulkGenerationProvenanceError(
            "Typed render backend diagnostics do not match the configured provider"
        )


def _generated_mono_pcm(pcm):
    """Normalize typed renderer PCM without flattening channels into time."""
    samples = np.asarray(pcm, dtype=np.float32)
    if samples.ndim == 1:
        mono = samples
    elif samples.ndim == 2 and samples.shape[1] in {1, 2}:
        mono = samples[:, 0] if samples.shape[1] == 1 else samples.mean(axis=1)
    else:
        raise BulkGenerationError(
            "Typed render PCM must be frames or frames-by-one/two channels"
        )
    if not np.isfinite(mono).all():
        raise BulkGenerationError("Typed render PCM contains non-finite samples")
    return mono


def _guard_job_process(output_directory, process_checker):
    job_path = output_directory.parent / "job.json"
    if not job_path.is_file():
        return None
    job = _load_json(job_path, "pregeneration job")
    if job.get("status") == "running" and process_checker(job.get("pid")):
        raise BulkGenerationError(
            f"Pregeneration job is active in another process with PID {job.get('pid')}"
        )
    if job.get("status") != "running":
        return None
    return {
        "job": str(job_path.resolve()),
        "job_sha256": sha256_file(job_path),
        "pid": job.get("pid"),
        "recorded_status": "running",
        "detected_status": "interrupted",
        "detected_at": _now(),
    }


def _audio_relative_path(voice, queue_id):
    voice_slug = slugify(voice)
    if not voice_slug:
        raise BulkGenerationError(
            f"Voice cannot form a safe audio directory: {voice!r}"
        )
    digest = hashlib.sha256(queue_id.encode("utf-8")).hexdigest()[:24]
    return Path("audio") / voice_slug / f"{digest}.wav"


def _archive_interrupted_artifact(output_directory, source):
    source = Path(source)
    digest = sha256_file(source)[:12]
    archive_root = _within(
        output_directory, Path("interrupted"), "Interrupted artifact directory"
    )
    archive_root.mkdir(parents=True, exist_ok=True)
    stem = slugify(source.name) or "artifact"
    candidate = archive_root / f"{stem}-{digest}{source.suffix}"
    suffix = 2
    while candidate.exists():
        if sha256_file(candidate) == sha256_file(source):
            source.unlink()
            return candidate
        candidate = archive_root / f"{stem}-{digest}-{suffix}{source.suffix}"
        suffix += 1
    os.replace(source, candidate)
    return candidate


def _safe_relative(value, label):
    if not isinstance(value, str) or not value:
        raise BulkGenerationError(f"{label} must be a relative POSIX path")
    if "\\" in value:
        raise BulkGenerationError(f"{label} must use POSIX separators")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BulkGenerationError(f"{label} must stay within generation output")
    return path


def _within(root, relative, label):
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise BulkGenerationError(
            f"{label} must stay within generation output"
        ) from error
    return candidate


def _load_stable_queue(queue_path):
    try:
        payload = queue_path.read_bytes()
    except OSError as error:
        raise BulkGenerationError(str(error)) from error
    digest = hashlib.sha256(payload).hexdigest()
    try:
        with TemporaryDirectory() as directory:
            snapshot = Path(directory) / "queue.jsonl"
            snapshot.write_bytes(payload)
            queue = VoiceGenerationQueue.load(snapshot)
    except (OSError, VoiceGenerationQueueError) as error:
        raise BulkGenerationError(str(error)) from error
    return queue, digest


def _snapshot_control_files(control_files):
    if not isinstance(control_files, dict):
        raise BulkGenerationError(
            "Generation control files must be a role/path mapping"
        )
    snapshots = []
    for role, configured in sorted(control_files.items()):
        role = _required_text(role, "Control-file role")
        expected = None
        path = configured
        if isinstance(configured, tuple) and len(configured) == 2:
            path, expected = configured
        path = Path(path).expanduser().resolve()
        try:
            digest = sha256_control_path(path)
        except OSError as error:
            raise BulkGenerationError(
                f"Unable to read generation control {role!r} {path}: {error}"
            ) from error
        if expected is not None and digest != expected:
            raise BulkGenerationSourceChangedError(
                f"Generation control {role!r} changed before the run started"
            )
        directory_files = _control_directory_files(path) if path.is_dir() else ()
        if directory_files and _control_directory_digest(directory_files) != digest:
            raise BulkGenerationSourceChangedError(
                f"Generation control {role!r} changed while it was inventoried"
            )
        snapshots.append(
            {
                "role": role,
                "path": path,
                "sha256": digest,
                "kind": "directory" if path.is_dir() else "file",
                "files": directory_files,
            }
        )
    return tuple(snapshots)


def _control_directory_files(path):
    records = []
    try:
        candidates = sorted(path.rglob("*"), key=lambda value: value.as_posix())
        for candidate in candidates:
            if not candidate.is_file():
                continue
            records.append(
                {
                    "path": candidate.relative_to(path).as_posix(),
                    "sha256": sha256_file(candidate),
                }
            )
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to inventory generation control directory {path}: {error}"
        ) from error
    return tuple(records)


def _control_directory_digest(records):
    digest = hashlib.sha256()
    for record in records:
        relative = record["path"].encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(record["sha256"]))
    return digest.hexdigest()


def _stored_control(control):
    record = {
        "role": control["role"],
        "kind": control["kind"],
        "path": str(control["path"]),
        "sha256": control["sha256"],
    }
    if control["kind"] == "directory":
        record["files"] = list(control["files"])
    return record


def _assert_sources_unchanged(queue_path, queue_sha256, controls):
    try:
        current_queue = sha256_file(queue_path)
    except OSError as error:
        raise BulkGenerationSourceChangedError(
            f"Generation queue became unreadable during the run: {error}"
        ) from error
    if current_queue != queue_sha256:
        raise BulkGenerationSourceChangedError(
            "Generation queue changed during the run; state was not published"
        )
    _assert_control_files_unchanged(controls)


def _assert_workspace_output_identity(output_directory, identity):
    if not isinstance(identity, dict) or set(identity) != {"path", "device", "inode"}:
        raise BulkGenerationSourceChangedError("Workspace output identity is malformed")
    path = Path(output_directory).expanduser()
    absolute = Path(os.path.abspath(os.fspath(path)))
    expected = Path(identity["path"])
    if absolute != expected or path.is_symlink():
        raise BulkGenerationSourceChangedError(
            "Workspace output directory changed or leaves its workspace"
        )
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise BulkGenerationSourceChangedError(
            f"Workspace output directory became unavailable: {error}"
        ) from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != identity["device"]
        or metadata.st_ino != identity["inode"]
    ):
        raise BulkGenerationSourceChangedError(
            "Workspace output directory identity changed"
        )


def _assert_control_files_unchanged(controls):
    for control in controls:
        try:
            digest = sha256_control_path(control["path"])
        except (BulkGenerationError, OSError) as error:
            raise BulkGenerationSourceChangedError(
                f"Generation control {control['role']!r} became unreadable: {error}"
            ) from error
        if digest != control["sha256"]:
            raise BulkGenerationSourceChangedError(
                f"Generation control {control['role']!r} changed during the run"
            )


def _load_json(path, description):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BulkGenerationError(
            f"Unable to read {description} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise BulkGenerationError(f"{description.capitalize()} must be a JSON object")
    return value


def _canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise BulkGenerationError(f"{label} must be non-empty text")
    return value.strip()


def _required_sha256(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BulkGenerationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise BulkGenerationError(f"{label} must be an integer")
    return value


def _nonnegative_int(value, label):
    value = _integer(value, label)
    if value < 0:
        raise BulkGenerationError(f"{label} cannot be negative")
    return value


def _nonnegative_optional_int(value, label):
    if value is None:
        return None
    return _nonnegative_int(value, label)


def _now():
    return datetime.now(timezone.utc).isoformat()

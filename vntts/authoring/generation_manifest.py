"""Leaf validation and projection for generated-audio manifests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, TypeAlias, TypedDict, TypeGuard

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import (
    PCM16_MONO_WAV_FORMAT,
    Pcm16MonoWavError,
    read_pcm16_mono_wav,
)
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import (
    GENERATED_AUDIO_SCHEMA,
    GENERATED_AUDIO_SCHEMA_VERSION,
    GeneratedAudioManifestError,
    write_generated_audio_manifest,
)
from vntts_artifacts.voice_manifest import (
    normalize_character_name,
    validate_voice_manifest,
)

from vntts.authoring.generation_lease import BulkGenerationError
from vntts.voices import pocket_tts_preset_voices


@dataclass(frozen=True)
class AudioQuality:
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_count: int
    peak: float


class _ControlSnapshot(TypedDict):
    role: str
    kind: str
    path: Path
    sha256: str


class _RecordedVoice(TypedDict):
    source_character: str
    speaker: str
    reference_sha256s: list[str]


_JsonObject: TypeAlias = dict[str, object]
_GenerationResult: TypeAlias = _JsonObject
_GenerationState: TypeAlias = _JsonObject


class _Pcm16MonoWavInfo(Protocol):
    duration_seconds: float
    peak: float
    sample_count: int
    sample_rate: int


def _is_generation_items(value: object) -> TypeGuard[dict[str, _GenerationResult]]:
    return isinstance(value, dict) and all(
        isinstance(queue_id, str) and isinstance(result, dict)
        for queue_id, result in value.items()
    )


def _generation_items(state: _GenerationState) -> dict[str, _GenerationResult]:
    items = state["items"]
    if not _is_generation_items(items):
        raise BulkGenerationError("Generation state items are malformed")
    return items


def snapshot_recorded_voices(
    controls: list[_ControlSnapshot], *, narrator_character: str | None = None
) -> dict[str, _RecordedVoice]:
    """Keep display identities from the same immutable inputs used for synthesis."""
    manifest = next(
        (control for control in controls if control["role"] == "voice_manifest"), None
    )
    if manifest is None or manifest["kind"] != "file":
        return {}
    try:
        payload = manifest["path"].read_bytes()
        if hashlib.sha256(payload).hexdigest() != manifest["sha256"]:
            raise BulkGenerationError("Recorded voice manifest changed during capture")
        document = json.loads(payload)
        voices = validate_voice_manifest(document)
        references = {
            control["path"]: control["sha256"]
            for control in controls
            if control["kind"] == "file"
            and control["role"].startswith(("voice_reference:", "narrator_selection:"))
        }
        result: dict[str, _RecordedVoice] = {}
        reference_paths: dict[str, tuple[Path, ...]] = {}
        for raw, voice in zip(document["voices"], voices, strict=True):
            # All current cloning backends use the first reference, not the pool.
            paths: tuple[Path, ...] = tuple(
                (manifest["path"].parent / value).resolve()
                for value in voice.references
            )
            reference: Path | None = paths[0] if paths else None
            digest = references.get(reference) if reference is not None else None
            if reference is not None and digest is None:
                continue
            if reference is None and voice.speaker not in pocket_tts_preset_voices:
                continue
            source = raw.get("vntts.source_character", voice.character)
            if not isinstance(source, str) or not source.strip():
                continue
            identity: _RecordedVoice = {
                "source_character": source.strip(),
                "speaker": voice.speaker,
                "reference_sha256s": [digest] if digest else [],
            }
            for name in (voice.character, *voice.aliases):
                result[normalize_character_name(name)] = identity
                reference_paths[normalize_character_name(name)] = paths
        narrator_controls = [
            control
            for control in controls
            if control["role"].startswith("narrator_selection:")
        ]
        if len(narrator_controls) == 1:
            narrator_character = narrator_controls[0]["role"].removeprefix(
                "narrator_selection:"
            )
        selected = result.get(normalize_character_name(narrator_character or ""))
        if selected is not None and len(narrator_controls) == 1:
            control = narrator_controls[0]
            if control["path"] not in reference_paths.get(
                normalize_character_name(narrator_character or ""), ()
            ):
                selected = None
            else:
                selected = {
                    "source_character": selected["source_character"],
                    "speaker": selected["speaker"],
                    "reference_sha256s": [control["sha256"]],
                }
        # Without an explicit narrator selection, the backend may use another default.
        result.pop("narrator", None)
        if selected is not None:
            result["narrator"] = selected
        return result
    except OSError, ValueError:
        # Legacy/custom producers may bind a different control format.
        return {}


def inspect_generated_wav(
    path: Path | str, *, allow_short_audio_event: bool = False
) -> AudioQuality:
    """Validate the normalized generated-audio WAV contract."""
    try:
        _samples, info = read_pcm16_mono_wav(path)
    except (OSError, Pcm16MonoWavError) as error:
        raise BulkGenerationError(
            f"Generated output is not a readable PCM16 mono WAV: {error}"
        ) from error
    return _audio_quality(info, allow_short_audio_event=allow_short_audio_event)


def _audio_quality(
    info: _Pcm16MonoWavInfo, *, allow_short_audio_event: bool = False
) -> AudioQuality:
    if info.sample_rate < 16_000:
        raise BulkGenerationError("Generated WAV sample rate must be at least 16 kHz")
    minimum_duration = 0.02 if allow_short_audio_event else 0.1
    if info.duration_seconds < minimum_duration or info.duration_seconds > 180:
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


def approved_manifest_entries(
    state: _GenerationState,
    output_directory: Path | str,
    *,
    validate_files: bool = True,
) -> list[dict[str, object]]:
    """Project approved state items into stable generated-audio entries."""
    entries = []
    for queue_id, result in _generation_items(state).items():
        if (
            result.get("status") != "approved"
            or result.get("review_status") != "approved"
        ):
            continue
        relative = safe_generation_relative_path(
            result.get("path"), f"State item {queue_id!r} path"
        )
        if validate_files:
            audio = contained_generation_path(
                output_directory, relative, "Generated WAV"
            )
            quality = validate_success_file(queue_id, result, audio)
            sample_rate = quality.sample_rate
            sample_count = quality.sample_count
        else:
            stored_quality = result.get("quality")
            if not isinstance(stored_quality, dict):
                raise BulkGenerationError(
                    f"Generated WAV quality is missing for {queue_id!r}"
                )
            sample_rate = _nonnegative_int(
                stored_quality.get("sample_rate"),
                f"State item {queue_id!r} sample_rate",
            )
            sample_count = _nonnegative_int(
                stored_quality.get("sample_count"),
                f"State item {queue_id!r} sample_count",
            )
        entry = {
            "queue_id": queue_id,
            "line_id": result["line_id"],
            "text_sha256": result["text_sha256"],
            "audio": relative.as_posix(),
            "audio_format": PCM16_MONO_WAV_FORMAT,
            "audio_sha256": result["file_sha256"],
            "sample_rate": sample_rate,
            "sample_count": sample_count,
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
            "synthesis_configuration",
            "synthesis_text_sha256",
            "text_transform",
            "speaker",
            "requested_voice_character",
            "voice_character",
            "synthesis_fallback",
            "narrator_character",
            "speech_quality",
            "carry_forward",
            "failure_repair",
            "synthesis_text_sha256",
            "attempts",
            "attempts_by_provider",
            "cohort_review",
            "outcome_merge",
            "terminal_conflict_resolution",
            "seed_applied",
            "audio_event_composition",
            "vntts.recorded_voice",
        ):
            if field in result:
                entry[field] = result.get(field)
        entries.append(entry)
    entries.sort(key=lambda entry: (entry["line_id"], entry["text_sha256"]))
    return entries


def write_generated_manifest_from_state(
    state: _GenerationState,
    output_directory: Path | str,
    manifest_path: Path | str,
    *,
    entries: list[dict[str, object]] | None = None,
    validate_files: bool = True,
) -> None:
    """Atomically publish the approved-only projection of one generation state."""
    entries = (
        approved_manifest_entries(state, output_directory)
        if entries is None
        else entries
    )
    metadata = {
        "game": state.get("game"),
        "language": state.get("language"),
        "source_queue_sha256": state["queue_sha256"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        if validate_files:
            write_generated_audio_manifest(manifest_path, metadata, entries)
        else:
            atomic_write_json(
                manifest_path,
                {
                    **metadata,
                    "schema": GENERATED_AUDIO_SCHEMA,
                    "schema_version": GENERATED_AUDIO_SCHEMA_VERSION,
                    "entry_count": len(entries),
                    "entries": entries,
                },
            )
    except GeneratedAudioManifestError as error:
        raise BulkGenerationError(str(error)) from error


def validate_success_file(
    queue_id: str, result: _GenerationResult, audio: Path
) -> AudioQuality:
    """Validate one generated WAV against its authoritative state record."""
    quality, _samples = validate_success_file_with_samples(queue_id, result, audio)
    return quality


def validate_success_file_with_samples(
    queue_id: str, result: _GenerationResult, audio: Path
) -> tuple[AudioQuality, object]:
    """Validate one WAV and retain its already-read samples for deeper checks."""
    if not audio.is_file():
        raise BulkGenerationError(f"Generated WAV is missing for {queue_id!r}: {audio}")
    if sha256_file(audio) != result.get("file_sha256"):
        raise BulkGenerationError(f"Generated WAV checksum mismatch for {queue_id!r}")
    try:
        samples, info = read_pcm16_mono_wav(audio)
    except (OSError, Pcm16MonoWavError) as error:
        raise BulkGenerationError(
            f"Generated output is not a readable PCM16 mono WAV: {error}"
        ) from error
    quality = _audio_quality(
        info,
        allow_short_audio_event=(result.get("provider") == "original-game-audio-event"),
    )
    stored = result.get("quality")
    if not isinstance(stored, dict):
        raise BulkGenerationError(f"Generated WAV quality is missing for {queue_id!r}")
    expected = asdict(quality)
    for field, value in expected.items():
        if stored.get(field) != value:
            raise BulkGenerationError(
                f"Generated WAV quality {field} mismatch for {queue_id!r}"
            )
    return quality, samples


def safe_generation_relative_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise BulkGenerationError(f"{label} must be a relative POSIX path")
    if "\\" in value:
        raise BulkGenerationError(f"{label} must use POSIX separators")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BulkGenerationError(f"{label} must stay within generation output")
    return path


def contained_generation_path(root: Path | str, relative: Path, label: str) -> Path:
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise BulkGenerationError(
            f"{label} must stay within generation output"
        ) from error
    return candidate


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise BulkGenerationError(f"{label} must be an integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    value = _integer(value, label)
    if value < 0:
        raise BulkGenerationError(f"{label} must be nonnegative")
    return value


__all__ = [
    "AudioQuality",
    "approved_manifest_entries",
    "contained_generation_path",
    "inspect_generated_wav",
    "safe_generation_relative_path",
    "validate_success_file",
    "write_generated_manifest_from_state",
]

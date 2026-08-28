"""Leaf validation and projection for generated-audio manifests."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import (
    PCM16_MONO_WAV_FORMAT,
    Pcm16MonoWavError,
    probe_pcm16_mono_wav,
)
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import (
    GENERATED_AUDIO_SCHEMA,
    GENERATED_AUDIO_SCHEMA_VERSION,
    GeneratedAudioManifestError,
    write_generated_audio_manifest,
)

from vntts.authoring.generation_lease import BulkGenerationError


@dataclass(frozen=True)
class AudioQuality:
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_count: int
    peak: float


def inspect_generated_wav(path, *, allow_short_audio_event=False):
    """Validate the normalized generated-audio WAV contract."""
    try:
        info = probe_pcm16_mono_wav(path)
    except (OSError, Pcm16MonoWavError) as error:
        raise BulkGenerationError(
            f"Generated output is not a readable PCM16 mono WAV: {error}"
        ) from error
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


def approved_manifest_entries(state, output_directory, *, validate_files=True):
    """Project approved state items into stable generated-audio entries."""
    entries = []
    for queue_id, result in state["items"].items():
        if (
            result.get("status") != "approved"
            or result.get("review_status") != "approved"
        ):
            continue
        relative = _safe_relative(result.get("path"), f"State item {queue_id!r} path")
        if validate_files:
            audio = _within(output_directory, relative, "Generated WAV")
            quality = validate_success_file(queue_id, result, audio)
            sample_rate = quality.sample_rate
            sample_count = quality.sample_count
        else:
            quality = result.get("quality")
            if not isinstance(quality, dict):
                raise BulkGenerationError(
                    f"Generated WAV quality is missing for {queue_id!r}"
                )
            sample_rate = _nonnegative_int(
                quality.get("sample_rate"),
                f"State item {queue_id!r} sample_rate",
            )
            sample_count = _nonnegative_int(
                quality.get("sample_count"),
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
        ):
            if field in result:
                entry[field] = result[field]
        entries.append(entry)
    entries.sort(key=lambda entry: (entry["line_id"], entry["text_sha256"]))
    return entries


def write_generated_manifest_from_state(
    state,
    output_directory,
    manifest_path,
    *,
    entries=None,
    validate_files=True,
):
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


def validate_success_file(queue_id, result, audio):
    """Validate one generated WAV against its authoritative state record."""
    if not audio.is_file():
        raise BulkGenerationError(f"Generated WAV is missing for {queue_id!r}: {audio}")
    if sha256_file(audio) != result.get("file_sha256"):
        raise BulkGenerationError(f"Generated WAV checksum mismatch for {queue_id!r}")
    quality = inspect_generated_wav(
        audio,
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
    return quality


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


def _integer(value, label):
    if not isinstance(value, int) or isinstance(value, bool):
        raise BulkGenerationError(f"{label} must be an integer")
    return value


def _nonnegative_int(value, label):
    value = _integer(value, label)
    if value < 0:
        raise BulkGenerationError(f"{label} must be nonnegative")
    return value


__all__ = [
    "AudioQuality",
    "approved_manifest_entries",
    "inspect_generated_wav",
    "validate_success_file",
    "write_generated_manifest_from_state",
]

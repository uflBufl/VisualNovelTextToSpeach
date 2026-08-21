"""Checksum-bound blind comparison for experimental internal-silence compression."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
import wave
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import Pcm16MonoWavError, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.failure_repair import (
    DEFAULT_INTERNAL_SILENCE_TARGET_SECONDS,
    compress_single_sentence_boundary_silence,
)
from vntts.authoring.game_pack import _rename_directory_no_replace
from vntts.authoring.listening import (
    ModelListeningError,
    create_listening_session_from_reports,
)

SILENCE_COMPARISON_SCHEMA = "vntts.authoring-silence-comparison"
SILENCE_COMPARISON_VERSION = 1


class SilenceComparisonError(RuntimeError):
    """A silence-compression comparison cannot be published or trusted."""


@dataclass(frozen=True)
class SilenceComparisonSample:
    queue_id: str
    line_id: str
    text: str
    raw_audio: Path
    segmented_audio: Path


@dataclass(frozen=True)
class SilenceComparisonResult:
    directory: Path
    sample_count: int
    report_paths: tuple[Path, Path]


def publish_silence_comparison(
    samples,
    output_directory,
    *,
    target_seconds=DEFAULT_INTERNAL_SILENCE_TARGET_SECONDS,
):
    """Publish immutable segmentation/compression reports for later blind review."""
    values = tuple(_validate_sample(value) for value in samples)
    if not values:
        raise SilenceComparisonError("Silence comparison requires at least one sample")
    queue_ids = [value.queue_id for value in values]
    if len(set(queue_ids)) != len(queue_ids):
        raise SilenceComparisonError("Silence comparison queue IDs must be unique")

    output = _new_directory(output_directory)
    if output.exists() or output.is_symlink():
        raise SilenceComparisonError(
            f"Silence comparison destination already exists: {output}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    checked_sources = []
    records = []
    segmented_report_samples = []
    compressed_report_samples = []
    try:
        for value in values:
            raw_path, raw_payload, raw_sha256, raw_pcm, raw_rate = _read_source_wav(
                value.raw_audio, "raw comparison audio"
            )
            (
                segmented_path,
                segmented_payload,
                segmented_sha256,
                _segmented_pcm,
                segmented_rate,
            ) = _read_source_wav(value.segmented_audio, "segmented comparison audio")
            if segmented_rate != raw_rate:
                raise SilenceComparisonError(
                    f"Comparison sample rates differ for {value.queue_id}"
                )
            try:
                compression = compress_single_sentence_boundary_silence(
                    raw_pcm,
                    raw_rate,
                    value.text,
                    target_seconds=target_seconds,
                )
            except ValueError as error:
                raise SilenceComparisonError(
                    f"Unsafe silence-compression sample {value.queue_id}: {error}"
                ) from error

            stem = hashlib.sha256(value.queue_id.encode("utf-8")).hexdigest()[:24]
            raw_relative = f"sources/{stem}-raw.wav"
            segmented_relative = f"audio/{stem}-segmentation.wav"
            compressed_relative = f"audio/{stem}-compression.wav"
            _write_exact(staging / raw_relative, raw_payload)
            _write_exact(staging / segmented_relative, segmented_payload)
            write_pcm16_wav(
                staging / compressed_relative,
                compression.pcm,
                raw_rate,
            )
            compressed_sha256 = sha256_file(staging / compressed_relative)
            text_sha256 = hashlib.sha256(value.text.encode("utf-8")).hexdigest()
            common = {
                "id": value.queue_id,
                "line_id": value.line_id,
                "text": value.text,
                "text_sha256": text_sha256,
            }
            segmented_report_samples.append(
                {
                    **common,
                    "audio": f"../{segmented_relative}",
                    "audio_sha256": segmented_sha256,
                }
            )
            compressed_report_samples.append(
                {
                    **common,
                    "audio": f"../{compressed_relative}",
                    "audio_sha256": compressed_sha256,
                }
            )
            records.append(
                {
                    "queue_id": value.queue_id,
                    "line_id": value.line_id,
                    "text": value.text,
                    "text_sha256": text_sha256,
                    "raw_source": str(raw_path),
                    "raw_source_sha256": raw_sha256,
                    "raw_copy": raw_relative,
                    "segmented_source": str(segmented_path),
                    "segmented_source_sha256": segmented_sha256,
                    "segmented_copy": segmented_relative,
                    "compressed_audio": compressed_relative,
                    "compressed_audio_sha256": compressed_sha256,
                    "sample_rate": raw_rate,
                    "transform": {
                        key: value
                        for key, value in asdict(compression).items()
                        if key != "pcm"
                    },
                }
            )
            checked_sources.extend(
                (
                    (raw_path, raw_sha256, "raw comparison audio"),
                    (segmented_path, segmented_sha256, "segmented comparison audio"),
                )
            )

        reports = staging / "reports"
        reports.mkdir(parents=True)
        segmented_report = reports / "sentence-segmentation.json"
        compressed_report = reports / "silence-compression.json"
        atomic_write_json(
            segmented_report,
            _model_report(
                "sentence-segmentation",
                "independently rendered sentence segments",
                segmented_report_samples,
            ),
            sort_keys=True,
        )
        atomic_write_json(
            compressed_report,
            _model_report(
                "silence-compression",
                "center-only compression of one verified silent span",
                compressed_report_samples,
            ),
            sort_keys=True,
        )
        artifacts = []
        for path in sorted(
            value
            for value in staging.rglob("*")
            if value.is_file() and value.name != "comparison.json"
        ):
            artifacts.append(
                {
                    "path": path.relative_to(staging).as_posix(),
                    "sha256": sha256_file(path),
                }
            )
        document = {
            "schema": SILENCE_COMPARISON_SCHEMA,
            "schema_version": SILENCE_COMPARISON_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "policy": {
                "kind": "single_sentence_boundary_silence_compression",
                "production_enabled": False,
                "requires_blind_review": True,
                "target_seconds": target_seconds,
            },
            "reports": [
                "reports/sentence-segmentation.json",
                "reports/silence-compression.json",
            ],
            "samples": records,
            "artifacts": artifacts,
        }
        atomic_write_json(staging / "comparison.json", document, sort_keys=True)

        validation = staging / ".validation-session"
        try:
            create_listening_session_from_reports(
                (segmented_report, compressed_report), validation, seed=0
            )
        except ModelListeningError as error:
            raise SilenceComparisonError(str(error)) from error
        shutil.rmtree(validation)
        for path, digest, label in checked_sources:
            if sha256_file(path) != digest:
                raise SilenceComparisonError(f"{label.title()} changed during staging")
        _rename_directory_no_replace(staging, output)
        return SilenceComparisonResult(
            output,
            len(records),
            (
                output / "reports/sentence-segmentation.json",
                output / "reports/silence-compression.json",
            ),
        )
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def load_silence_comparison(directory):
    """Validate a published comparison and every checksum-bound artifact."""
    root = Path(directory).expanduser().resolve()
    try:
        document = json.loads((root / "comparison.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SilenceComparisonError(
            f"Unable to read silence comparison: {error}"
        ) from error
    if (
        not isinstance(document, dict)
        or document.get("schema") != SILENCE_COMPARISON_SCHEMA
        or document.get("schema_version") != SILENCE_COMPARISON_VERSION
    ):
        raise SilenceComparisonError("Unsupported silence comparison schema")
    reports = document.get("reports")
    samples = document.get("samples")
    artifacts = document.get("artifacts")
    if (
        reports
        != [
            "reports/sentence-segmentation.json",
            "reports/silence-compression.json",
        ]
        or not isinstance(samples, list)
        or not samples
        or not isinstance(artifacts, list)
    ):
        raise SilenceComparisonError("Silence comparison inventory is malformed")
    seen = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            raise SilenceComparisonError("Silence comparison artifact is malformed")
        relative = artifact["path"]
        if relative in seen:
            raise SilenceComparisonError("Silence comparison artifact is duplicated")
        path = _contained_file(root, relative)
        digest = artifact["sha256"]
        if not _is_sha256(digest) or sha256_file(path) != digest:
            raise SilenceComparisonError(
                f"Silence comparison artifact checksum changed: {relative}"
            )
        seen[relative] = digest
    if set(reports) - set(seen):
        raise SilenceComparisonError(
            "Silence comparison report inventory is incomplete"
        )
    required_sample_fields = {
        "queue_id",
        "line_id",
        "text",
        "text_sha256",
        "raw_source",
        "raw_source_sha256",
        "raw_copy",
        "segmented_source",
        "segmented_source_sha256",
        "segmented_copy",
        "compressed_audio",
        "compressed_audio_sha256",
        "sample_rate",
        "transform",
    }
    sample_ids = set()
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != required_sample_fields:
            raise SilenceComparisonError("Silence comparison sample is malformed")
        queue_id = sample["queue_id"]
        if not isinstance(queue_id, str) or not queue_id or queue_id in sample_ids:
            raise SilenceComparisonError("Silence comparison sample ID is invalid")
        sample_ids.add(queue_id)
        if (
            not isinstance(sample["line_id"], str)
            or not sample["line_id"]
            or not isinstance(sample["text"], str)
            or not sample["text"]
            or hashlib.sha256(sample["text"].encode("utf-8")).hexdigest()
            != sample["text_sha256"]
        ):
            raise SilenceComparisonError(
                "Silence comparison sample identity is invalid"
            )
        for path_field, digest_field in (
            ("raw_copy", "raw_source_sha256"),
            ("segmented_copy", "segmented_source_sha256"),
            ("compressed_audio", "compressed_audio_sha256"),
        ):
            relative = sample[path_field]
            if (
                not _is_sha256(sample[digest_field])
                or seen.get(relative) != sample[digest_field]
            ):
                raise SilenceComparisonError(
                    "Silence comparison sample is not bound to its artifact inventory"
                )
    return document


def create_silence_comparison_session(
    comparison_directory, output_directory, *, seed=0
):
    """Create a standard blind A/B session from one verified comparison bundle."""
    root = Path(comparison_directory).expanduser().resolve()
    document = load_silence_comparison(root)
    report_paths = tuple(_contained_file(root, value) for value in document["reports"])
    try:
        return create_listening_session_from_reports(
            report_paths, output_directory, seed=seed
        )
    except ModelListeningError as error:
        raise SilenceComparisonError(str(error)) from error


def _validate_sample(value):
    if not isinstance(value, SilenceComparisonSample):
        raise SilenceComparisonError(
            "Silence comparison samples must be SilenceComparisonSample values"
        )
    for field in ("queue_id", "line_id", "text"):
        text = getattr(value, field)
        if not isinstance(text, str) or not text or text != text.strip():
            raise SilenceComparisonError(f"Silence comparison {field} is invalid")
    return value


def _read_source_wav(path, label):
    source = Path(path).expanduser()
    if source.is_symlink():
        raise SilenceComparisonError(f"{label.title()} must not be a symlink")
    source = source.resolve()
    try:
        payload = source.read_bytes()
        with wave.open(io.BytesIO(payload), "rb") as wav:
            if (
                wav.getcomptype() != "NONE"
                or wav.getnchannels() != 1
                or wav.getsampwidth() != 2
                or wav.getframerate() < 1
            ):
                raise Pcm16MonoWavError("expected mono 16-bit PCM WAV")
            rate = wav.getframerate()
            count = wav.getnframes()
            pcm_payload = wav.readframes(count)
        pcm = np.frombuffer(pcm_payload, dtype="<i2")
        if len(pcm) != count:
            raise Pcm16MonoWavError("WAV sample data is incomplete")
    except (OSError, EOFError, wave.Error, Pcm16MonoWavError) as error:
        raise SilenceComparisonError(f"Unable to read {label}: {error}") from error
    return (
        source,
        payload,
        hashlib.sha256(payload).hexdigest(),
        pcm.astype(np.float32) / 32768.0,
        rate,
    )


def _model_report(model_id, model, samples):
    return {
        "schema": "vntts.voice-model-report",
        "schema_version": 1,
        "model_id": model_id,
        "provider": "derived-comparison",
        "backend": "derived-comparison",
        "model": model,
        "samples": samples,
    }


def _write_exact(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _new_directory(value):
    path = Path(value).expanduser()
    if not path.name or path.name in {".", ".."}:
        raise SilenceComparisonError("Silence comparison requires a directory name")
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.parent.resolve() / path.name


def _contained_file(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise SilenceComparisonError("Silence comparison path is invalid")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SilenceComparisonError("Silence comparison path leaves its directory")
    root = Path(root).resolve()
    unresolved = root / Path(*pure.parts)
    current = root
    for part in pure.parts:
        current /= part
        if current.is_symlink():
            raise SilenceComparisonError(
                "Silence comparison artifacts must not use symlinks"
            )
    path = unresolved.resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise SilenceComparisonError(
            "Silence comparison path leaves its directory"
        ) from error
    if not path.is_file():
        raise SilenceComparisonError(
            f"Silence comparison artifact is missing: {relative}"
        )
    return path


def _is_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )

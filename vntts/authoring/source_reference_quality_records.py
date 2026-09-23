"""Wire records and local decisions for source-reference quality review."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import struct
import zlib
from collections.abc import Generator, Iterable, Mapping, MutableSequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import Pcm16MonoWavError, probe_pcm16_mono_wav
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.advisory_lock import (
    AdvisoryLockBusyError,
    exclusive_advisory_lock,
)
from vntts.document_identity import is_lowercase_sha256
from vntts.path_safety import contained_regular_file

QUALITY_REVIEW_SCHEMA = "vntts.authoring-source-reference-quality-review"
QUALITY_REVIEW_VERSION = 1
QUALITY_DECISIONS = frozenset({"accept", "reject", "needs_sample"})
JsonObject = dict[str, object]


class SourceReferenceQualityError(RuntimeError):
    """Source-reference quality evidence is invalid or cannot be updated."""


@dataclass(frozen=True)
class SourceReferenceQualityResult:
    directory: Path
    variants: int
    generated_samples: int
    excluded_results: int

    @property
    def session(self) -> Path:
        return self.directory / "review.json"

    def to_dict(self) -> dict[str, object]:
        return {
            "directory": str(self.directory),
            "session": str(self.session),
            "variants": self.variants,
            "generated_samples": self.generated_samples,
            "excluded_results": self.excluded_results,
        }


# Keep public exception/result pickle and introspection identity at the original API.
SourceReferenceQualityError.__module__ = "vntts.authoring.source_reference_quality"
SourceReferenceQualityResult.__module__ = "vntts.authoring.source_reference_quality"


def load_source_reference_quality_review(path: str | Path) -> JsonObject:
    """Load and fully validate one self-contained quality review."""
    path = Path(path).expanduser().resolve()
    _payload, session = _read_json(path, "source-reference quality review")
    return validate_source_reference_quality_review_document(session, path.parent)


def validate_source_reference_quality_review_document(
    document: JsonObject, root: str | Path
) -> JsonObject:
    """Validate captured quality-review semantics against one artifact root."""
    session = copy.deepcopy(document)
    path = Path(root).expanduser().resolve() / "review.json"
    _validate_quality_review_header(session)
    variants = _quality_review_variants(session)
    seen: set[str] = set()
    completed = sum(
        _validate_quality_review_variant(path.parent, card, index, seen)
        for index, card in enumerate(variants)
    )
    if session.get("completed_count") != completed:
        raise SourceReferenceQualityError("Quality review progress is inconsistent")
    for field in (
        "source_reference_plan_sha256",
        "source_reference_evaluation_sha256",
        "generation_state_sha256",
    ):
        _required_sha256(session.get(field), f"quality review {field}")
    return session


def _validate_quality_review_header(session: JsonObject) -> None:
    if (
        session.get("schema") != QUALITY_REVIEW_SCHEMA
        or session.get("schema_version") != QUALITY_REVIEW_VERSION
    ):
        raise SourceReferenceQualityError(
            "Unsupported source-reference quality review schema"
        )
    _aware_timestamp(session.get("created_at"), "quality review created_at")
    _aware_timestamp(session.get("updated_at"), "quality review updated_at")


def _quality_review_variants(session: JsonObject) -> list[object]:
    variants = session.get("variants")
    if (
        not isinstance(variants, list)
        or not variants
        or session.get("variant_count") != len(variants)
    ):
        raise SourceReferenceQualityError("Quality review variant count is invalid")
    return variants


def _validate_quality_review_variant(
    root: Path, card: object, index: int, seen: set[str]
) -> int:
    if not isinstance(card, dict):
        raise SourceReferenceQualityError(
            f"Quality review variant {index} must be an object"
        )
    variant_id = _required_text(card.get("variant_id"), "quality variant ID")
    if variant_id in seen:
        raise SourceReferenceQualityError(
            f"Quality review variant is duplicated: {variant_id}"
        )
    seen.add(variant_id)
    _validate_variant_identity(card, variant_id)
    _validate_variant_reference_kind(card, variant_id)
    _validate_variant_portrait(root, card, variant_id)
    _positive_integer(
        card.get("affected_queue_item_count"),
        f"quality variant {variant_id} affected count",
    )
    _validate_audio_record(root, card.get("reference"), variant_id)
    _validate_decision_context(card.get("decision_context"), variant_id)
    generated, excluded = _validate_variant_outcomes(card, variant_id)
    _validate_variant_samples(root, generated, excluded, variant_id)
    return _validate_variant_decision(card.get("decision"), generated, variant_id)


def _validate_variant_identity(card: JsonObject, variant_id: str) -> None:
    for field in ("cluster_id", "character", "source_bank"):
        _required_text(card.get(field), f"quality variant {variant_id} {field}")


def _validate_variant_reference_kind(card: JsonObject, variant_id: str) -> None:
    reference_kind = card.get("reference_kind", "single_media")
    if reference_kind == "single_media":
        media_id = card.get("media_id")
        if isinstance(media_id, bool) or not isinstance(media_id, int) or media_id < 0:
            raise SourceReferenceQualityError(
                f"Quality variant {variant_id} media ID is invalid"
            )
        return
    if reference_kind == "exact_bank_composite":
        media_ids = card.get("media_ids")
        if (
            not isinstance(media_ids, list)
            or len(media_ids) < 2
            or any(
                isinstance(media_id, bool)
                or not isinstance(media_id, int)
                or media_id < 0
                for media_id in media_ids
            )
            or len(media_ids) != len(set(media_ids))
        ):
            raise SourceReferenceQualityError(
                f"Quality variant {variant_id} composite media IDs are invalid"
            )
        return
    raise SourceReferenceQualityError(
        f"Quality variant {variant_id} reference kind is invalid"
    )


def _validate_variant_portrait(root: Path, card: JsonObject, variant_id: str) -> None:
    portrait = card.get("portrait")
    if portrait is not None and (not isinstance(portrait, str) or not portrait.strip()):
        raise SourceReferenceQualityError(
            f"Quality variant {variant_id} portrait is invalid"
        )
    portrait_image = card.get("portrait_image")
    if portrait_image is not None:
        _validate_portrait_record(root, portrait_image, variant_id)


def _validate_decision_context(value: object, variant_id: str) -> None:
    if value is None:
        return
    if (
        not isinstance(value, dict)
        or set(value) != {"backend", "model", "generation_profile", "seed"}
        or any(
            not isinstance(value.get(field), str) or not value[field].strip()
            for field in ("backend", "model", "generation_profile")
        )
        or not isinstance(value.get("seed"), (str, int))
        or isinstance(value.get("seed"), bool)
    ):
        raise SourceReferenceQualityError(
            f"Quality variant {variant_id} decision context is invalid"
        )


def _validate_variant_outcomes(
    card: JsonObject, variant_id: str
) -> tuple[list[object], list[object]]:
    generated = card.get("generated_samples")
    excluded = card.get("excluded_results")
    if not isinstance(generated, list) or not isinstance(excluded, list):
        raise SourceReferenceQualityError(
            f"Quality variant {variant_id} outcomes are invalid"
        )
    return generated, excluded


def _validate_variant_samples(
    root: Path,
    generated: list[object],
    excluded: list[object],
    variant_id: str,
) -> None:
    queue_ids: set[str] = set()
    for sample in generated:
        _add_quality_sample_queue_id(
            queue_ids,
            _validate_sample(root, sample, variant_id, audio=True),
            variant_id,
        )
    for sample in excluded:
        queue_id = _validate_sample(root, sample, variant_id, audio=False)
        _add_quality_sample_queue_id(queue_ids, queue_id, variant_id)
        if not isinstance(sample, dict):
            raise SourceReferenceQualityError(
                f"Quality variant {variant_id} sample must be an object"
            )
        _required_text(sample.get("status"), f"excluded {queue_id} status")
        attempts = sample.get("attempts")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise SourceReferenceQualityError(
                f"Excluded result {queue_id} attempts are invalid"
            )


def _add_quality_sample_queue_id(
    queue_ids: set[str], queue_id: str, variant_id: str
) -> None:
    if queue_id in queue_ids:
        raise SourceReferenceQualityError(
            f"Quality variant {variant_id} queue ID is duplicated"
        )
    queue_ids.add(queue_id)


def _validate_variant_decision(
    value: object, generated: list[object], variant_id: str
) -> int:
    if value is None:
        return 0
    if not isinstance(value, dict) or value.get("decision") not in QUALITY_DECISIONS:
        raise SourceReferenceQualityError(
            f"Quality variant {variant_id} decision is invalid"
        )
    _aware_timestamp(
        value.get("reviewed_at"), f"quality variant {variant_id} reviewed_at"
    )
    if value["decision"] == "accept" and not generated:
        raise SourceReferenceQualityError(
            f"Quality variant {variant_id} was accepted without generated audio"
        )
    return 1


def quality_review_progress(session: Mapping[str, object]) -> tuple[int, int]:
    variants = _variants(session)
    completed = sum(card.get("decision") is not None for card in variants)
    return completed, len(variants)


def next_pending_quality_variant(
    session: Mapping[str, object],
) -> JsonObject | None:
    return next(
        (card for card in _variants(session) if card.get("decision") is None), None
    )


def record_source_reference_quality_decision(
    session_path: str | Path, variant_id: str, decision: str, *, overwrite: bool = False
) -> JsonObject:
    if decision not in QUALITY_DECISIONS:
        raise SourceReferenceQualityError(
            "Quality decision must be accept, reject, or needs_sample"
        )
    session_path = Path(session_path).expanduser().resolve()
    with _decision_lock(session_path):
        try:
            original_payload = session_path.read_bytes()
        except OSError as error:
            raise SourceReferenceQualityError(str(error)) from error
        session = load_source_reference_quality_review(session_path)
        if session_path.read_bytes() != original_payload:
            raise SourceReferenceQualityError(
                "Quality review changed while the decision was loaded"
            )
        card = next(
            (
                item
                for item in _variants(session)
                if _required_text(item.get("variant_id"), "quality variant ID")
                == variant_id
            ),
            None,
        )
        if card is None:
            raise SourceReferenceQualityError(f"Unknown quality variant: {variant_id}")
        if card.get("decision") is not None and not overwrite:
            raise SourceReferenceQualityError(
                f"Quality variant is already rated: {variant_id}"
            )
        generated_samples = card.get("generated_samples")
        if not isinstance(generated_samples, list):
            raise SourceReferenceQualityError("Quality variant outcomes are invalid")
        if decision == "accept" and not generated_samples:
            raise SourceReferenceQualityError(
                "A reference without generated samples cannot be accepted"
            )
        card["decision"] = {"decision": decision, "reviewed_at": _utc_now()}
        session["completed_count"] = quality_review_progress(session)[0]
        session["updated_at"] = _utc_now()
        if session_path.read_bytes() != original_payload:
            raise SourceReferenceQualityError(
                "Quality review changed before the decision was saved"
            )
        atomic_write_json(session_path, session, sort_keys=True)
        return session


def accepted_source_reference_variants(
    session: Mapping[str, object], *, require_complete: bool = True
) -> tuple[str, ...]:
    completed, total = quality_review_progress(session)
    if require_complete and completed != total:
        raise SourceReferenceQualityError("Quality review is incomplete")
    accepted: list[str] = []
    for card in _variants(session):
        decision = card.get("decision")
        if isinstance(decision, dict) and decision.get("decision") == "accept":
            accepted.append(
                _required_text(card.get("variant_id"), "quality variant ID")
            )
    return tuple(accepted)


def capture_quality_outcomes(
    outcomes: Iterable[tuple[JsonObject, object, Path]],
    state_directory: Path,
    staging: Path,
    snapshots: MutableSequence[tuple[Path, str]],
    *,
    error_type: type[Exception] = SourceReferenceQualityError,
    generated_label: str = "Generated evaluation audio",
) -> tuple[list[JsonObject], list[JsonObject]]:
    """Copy checksum-bound generated samples and retain other outcomes as evidence."""
    generated: list[JsonObject] = []
    excluded: list[JsonObject] = []
    for common, result, relative in outcomes:
        queue_id = _capture_text(
            common.get("queue_id"), "Quality sample queue ID", error_type
        )
        result_document = result if isinstance(result, Mapping) else {}
        if result_document.get("status", "pending") not in {"generated", "approved"}:
            failure = result_document.get("failure")
            excluded.append(
                {
                    **common,
                    "status": result_document.get("status", "pending"),
                    "attempts": result_document.get("attempts", 0),
                    "error": _optional_text(result_document.get("last_error")),
                    "completion": _optional_failure_text(failure, "completion"),
                    "failure_kind": _optional_failure_text(failure, "kind"),
                }
            )
            continue
        source = contained_regular_file(
            state_directory,
            _capture_text(
                result_document.get("path"),
                f"{generated_label} {queue_id} path",
                error_type,
            ),
            f"{generated_label} {queue_id}",
            error_type=error_type,
        )
        digest = _capture_sha256(
            result_document.get("file_sha256"),
            f"{generated_label} {queue_id} hash",
            error_type,
        )
        if sha256_file(source) != digest:
            raise error_type(f"{generated_label} changed: {queue_id}")
        snapshots.append((source, digest))
        audio = _copy_audio(source, digest, staging / relative)
        generated.append({**common, "audio": relative.as_posix(), **audio})
    return generated, excluded


def _copy_audio(source: Path, digest: str, destination: Path) -> JsonObject:
    try:
        info = probe_pcm16_mono_wav(source)
    except Pcm16MonoWavError as error:
        raise SourceReferenceQualityError(
            f"Invalid review WAV {source}: {error}"
        ) from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if sha256_file(destination) != digest:
        raise SourceReferenceQualityError(f"Review WAV changed while copied: {source}")
    return {
        "audio_sha256": digest,
        "sample_rate": info.sample_rate,
        "sample_count": info.sample_count,
        "duration_seconds": round(info.duration_seconds, 6),
    }


def _validate_portrait_record(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, dict):
        raise SourceReferenceQualityError(f"Quality portrait {label} must be an object")
    path = _contained_file(root, value.get("image"), f"quality portrait {label}")
    digest = _required_sha256(
        value.get("image_sha256"), f"quality portrait {label} hash"
    )
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise SourceReferenceQualityError(
            f"Unable to read quality portrait {label}: {error}"
        ) from error
    if hashlib.sha256(payload).hexdigest() != digest:
        raise SourceReferenceQualityError(f"Quality portrait changed: {label}")
    width, height = _probe_png(payload, f"quality portrait {label}")
    if value.get("width") != width or value.get("height") != height:
        raise SourceReferenceQualityError(f"Quality portrait metadata changed: {label}")
    return path


def _probe_png(payload: bytes, label: str) -> tuple[int, int]:
    if not isinstance(payload, bytes) or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SourceReferenceQualityError(f"{label.title()} is not a PNG")
    offset = 8
    dimensions: tuple[int, int] | None = None
    idat_parts: list[bytes] = []
    saw_iend = False
    while offset < len(payload):
        kind, data, chunk_end = _read_png_chunk(payload, offset, label)
        if offset == 8:
            dimensions = _png_dimensions(kind, data, label)
        elif kind == b"IDAT":
            idat_parts.append(data)
        elif kind == b"IEND":
            _validate_png_iend(data, chunk_end, len(payload), label)
            saw_iend = True
        offset = chunk_end
    if dimensions is None or not idat_parts or not saw_iend:
        raise SourceReferenceQualityError(f"{label.title()} is incomplete")
    _validate_png_image_data(idat_parts, label)
    return dimensions


def _read_png_chunk(
    payload: bytes, offset: int, label: str
) -> tuple[bytes, bytes, int]:
    if len(payload) - offset < 12:
        raise SourceReferenceQualityError(f"{label.title()} is truncated")
    length = struct.unpack(">I", payload[offset : offset + 4])[0]
    kind = payload[offset + 4 : offset + 8]
    chunk_end = offset + 12 + length
    if chunk_end > len(payload):
        raise SourceReferenceQualityError(f"{label.title()} is truncated")
    data = payload[offset + 8 : offset + 8 + length]
    expected_crc = struct.unpack(">I", payload[offset + 8 + length : chunk_end])[0]
    if zlib.crc32(kind + data) & 0xFFFFFFFF != expected_crc:
        raise SourceReferenceQualityError(f"{label.title()} has an invalid CRC")
    return kind, data, chunk_end


def _png_dimensions(kind: bytes, data: bytes, label: str) -> tuple[int, int]:
    if kind != b"IHDR" or len(data) != 13:
        raise SourceReferenceQualityError(f"{label.title()} has no valid IHDR")
    width, height = struct.unpack(">II", data[:8])
    if width < 1 or height < 1:
        raise SourceReferenceQualityError(f"{label.title()} has invalid dimensions")
    return width, height


def _validate_png_iend(data: bytes, chunk_end: int, size: int, label: str) -> None:
    if data or chunk_end != size:
        raise SourceReferenceQualityError(f"{label.title()} has invalid IEND")


def _validate_png_image_data(parts: list[bytes], label: str) -> None:
    try:
        decoded = zlib.decompress(b"".join(parts))
    except zlib.error as error:
        raise SourceReferenceQualityError(
            f"{label.title()} has invalid image data"
        ) from error
    if not decoded:
        raise SourceReferenceQualityError(f"{label.title()} has empty image data")


def _validate_audio_record(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, dict):
        raise SourceReferenceQualityError(f"Quality audio {label} must be an object")
    path = _contained_file(root, value.get("audio"), f"quality audio {label}")
    digest = _required_sha256(value.get("audio_sha256"), f"quality audio {label} hash")
    if sha256_file(path) != digest:
        raise SourceReferenceQualityError(f"Quality audio changed: {label}")
    try:
        info = probe_pcm16_mono_wav(path)
    except Pcm16MonoWavError as error:
        raise SourceReferenceQualityError(
            f"Invalid quality WAV {label}: {error}"
        ) from error
    if (
        value.get("sample_rate") != info.sample_rate
        or value.get("sample_count") != info.sample_count
        or value.get("duration_seconds") != round(info.duration_seconds, 6)
    ):
        raise SourceReferenceQualityError(f"Quality audio metadata changed: {label}")
    return path


def _validate_sample(
    root: Path, sample: object, variant_id: str, *, audio: bool
) -> str:
    if not isinstance(sample, dict):
        raise SourceReferenceQualityError(
            f"Quality variant {variant_id} sample must be an object"
        )
    queue_id = _required_text(sample.get("queue_id"), "quality sample queue ID")
    _required_text(sample.get("evaluation_kind"), f"quality sample {queue_id} kind")
    text = _required_text(sample.get("text"), f"quality sample {queue_id} text")
    digest = _required_sha256(
        sample.get("text_sha256"), f"quality sample {queue_id} text hash"
    )
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        raise SourceReferenceQualityError(f"Quality sample text changed: {queue_id}")
    if audio:
        _validate_audio_record(root, sample, queue_id)
    return queue_id


def _read_json(path: str | Path, label: str) -> tuple[bytes, JsonObject]:
    path = Path(path).expanduser().resolve()
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, json.JSONDecodeError) as error:
        raise SourceReferenceQualityError(
            f"Unable to read {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise SourceReferenceQualityError(f"{label.title()} must be an object")
    return payload, value


def _contained_file(root: Path, value: object, label: str) -> Path:
    value = _required_text(value, label)
    return Path(
        contained_regular_file(
            root, value, label, error_type=SourceReferenceQualityError
        )
    )


def _variants(session: Mapping[str, object]) -> list[JsonObject]:
    variants = session.get("variants")
    if not isinstance(variants, list) or any(
        not isinstance(card, dict) for card in variants
    ):
        raise SourceReferenceQualityError("Quality review variants are invalid")
    return variants


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceReferenceQualityError(f"{label.title()} must be non-empty text")
    return value.strip()


def _capture_text(value: object, label: str, error_type: type[Exception]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{label.title()} must be non-empty text")
    return value.strip()


def _capture_sha256(value: object, label: str, error_type: type[Exception]) -> str:
    value = _capture_text(value, label, error_type)
    if not is_lowercase_sha256(value):
        raise error_type(f"{label.title()} must be lowercase SHA-256")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_failure_text(value: object, field: str) -> str | None:
    return (
        value.get(field)
        if isinstance(value, Mapping) and isinstance(value.get(field), str)
        else None
    )


def _required_sha256(value: object, label: str) -> str:
    value = _required_text(value, label)
    if not is_lowercase_sha256(value):
        raise SourceReferenceQualityError(f"{label.title()} must be lowercase SHA-256")
    return value


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SourceReferenceQualityError(f"{label.title()} must be positive")
    return value


def _aware_timestamp(value: object, label: str) -> datetime:
    value = _required_text(value, label)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SourceReferenceQualityError(f"{label.title()} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceReferenceQualityError(f"{label.title()} must include a timezone")
    return parsed


@contextmanager
def _decision_lock(session_path: Path) -> Generator[None, None, None]:
    lock_path = session_path.with_name(f".{session_path.name}.lock")
    try:
        with exclusive_advisory_lock(lock_path):
            yield
    except AdvisoryLockBusyError as error:
        raise SourceReferenceQualityError(
            "Another source-reference decision is being saved"
        ) from error


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "QUALITY_DECISIONS",
    "QUALITY_REVIEW_SCHEMA",
    "QUALITY_REVIEW_VERSION",
    "SourceReferenceQualityError",
    "SourceReferenceQualityResult",
    "accepted_source_reference_variants",
    "capture_quality_outcomes",
    "load_source_reference_quality_review",
    "next_pending_quality_variant",
    "quality_review_progress",
    "record_source_reference_quality_decision",
    "validate_source_reference_quality_review_document",
]

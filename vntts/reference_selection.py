"""Frozen full-pool cloning reference ranking shared by the app and studies."""

from __future__ import annotations

import hashlib
import io
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path
from typing import Any, TypedDict

import numpy as np
from numpy.typing import NDArray

from vntts.authoring.speaker_identity import make_speechbrain_embedder
from vntts.authoring.speaker_identity_model import (
    REVISION,
    RUNTIME_VERSION,
    SpeakerIdentityModelError,
    install_managed_speaker_identity_model,
    managed_speaker_identity_installation,
    resolve_managed_speaker_identity_model,
)
from vntts.document_identity import sha256_file
from vntts.path_safety import read_regular_bytes
from vntts.pregeneration_voices import (
    STORY_LINE_ROUTE,
    ReferenceRanking,
    _candidate_inventory,
    _VoiceReferenceFacts,
    automatic_voice_reference_is_eligible,
    validated_player_voice_candidates,
)
from vntts.reference_quality import analyze_reference_bytes
from vntts.settings import AppSettings
from vntts.voices import (
    CharacterVoiceRegistry,
    normalize_character_name,
    read_voice_reference_bytes,
)


class Clip(TypedDict):
    path: str
    sha256: str
    source_bank: str | None
    source_line_ids: tuple[str, ...]
    quality_score: int
    seconds: float
    text: str


class ScoredClip(Clip):
    mean_peer_cosine: float | None
    mean_holdout_cosine: float | None


class StudyReport(TypedDict):
    manifest_sha256: str
    character: str
    encoder_revision: str
    holdout_bank: str | None
    fit_count: int
    holdout_count: int
    excluded_preflight: list[dict[str, object]]
    baseline: ScoredClip | None
    representative: ScoredClip | None
    originals_by_typicality: list[ScoredClip]
    renders: list[dict[str, object]]


class SelectionReport(StudyReport):
    selected: dict[str, Any] | None
    selection_method: str
    selection_reason: str
    steady_pool_count: int
    steady_candidates: list[dict[str, Any]]
    excluded_features: list[dict[str, str]]
    timing_seconds: dict[str, float]


type Vector = NDArray[np.float64]
type Encoder = Callable[[bytes], object]
type Catalog = tuple[CharacterVoiceRegistry, Sequence[dict[str, Any]]]

logger = logging.getLogger(__name__)


class ReferenceSelectionUnavailable(RuntimeError):
    """The automatic reference-ranking runtime is incomplete."""


def unit_embedding(embed: Encoder, payload: bytes) -> Vector:
    vector = np.asarray(embed(payload), dtype=np.float64).reshape(-1)
    norm = np.linalg.norm(vector)
    if not np.isfinite(norm) or norm == 0:
        raise ValueError("The speaker encoder returned an invalid embedding")
    return vector / norm


def typicality_scores(vectors: Sequence[Vector]) -> Vector:
    """Mean cosine to all other original clips, computed without a pairwise matrix."""
    if len(vectors) < 3:
        raise ValueError("Choosing a representative requires at least three originals")
    matrix = np.stack(vectors)
    return (matrix @ matrix.sum(axis=0) - 1) / (len(matrix) - 1)


def study(
    manifest: Path,
    character: str,
    holdout_bank: str | None,
    embed: Encoder,
    renders: Sequence[Path] = (),
    *,
    speech_backend: str = "moss-tts",
    check_cancelled: Callable[[], None] | None = None,
    _catalog: Catalog | None = None,
) -> StudyReport:
    if renders and not holdout_bank:
        raise ValueError(
            "Comparing renders requires independent originals (--holdout-bank)"
        )
    manifest = manifest.parent.resolve() / manifest.name
    manifest_digest = sha256_file(manifest)
    registry, variants = (
        validated_player_voice_candidates(manifest, speech_backend=speech_backend)
        if _catalog is None
        else _catalog
    )
    # Use the planner's deduplication and ordering before its three-preview limit.
    inventory = _candidate_inventory(
        character,
        None,
        AppSettings(speech_backend=speech_backend),
        registry,
        variants,
        reference_facts=_VoiceReferenceFacts(),
    )
    role_variants = [
        row
        for row in variants
        if row.get("candidate_origin") == STORY_LINE_ROUTE
        and normalize_character_name(str(row["character"]))
        == normalize_character_name(character)
    ]
    by_voice = {row["voice_character"]: row for row in role_variants}
    by_digest = {row["reference_sha256"]: row for row in role_variants}
    holdout_digests = {
        row["reference_sha256"]
        for row in role_variants
        if holdout_bank and row.get("source_bank") == holdout_bank
    }
    pending: list[tuple[Clip, Path]] = []
    fit: list[tuple[Clip, Vector | None]] = []
    held_out: list[Vector] = []
    excluded: list[dict[str, object]] = []
    for candidate in inventory:
        if check_cancelled is not None:
            check_cancelled()
        if candidate.candidate_origin != STORY_LINE_ROUTE:
            continue
        if not automatic_voice_reference_is_eligible(
            candidate_origin=candidate.candidate_origin,
            source_bank=candidate.source_bank,
            source_line_ids=candidate.source_line_ids,
            duration_seconds=candidate.reference_duration_seconds,
        ):
            continue
        voice = registry.resolve_source(candidate.source_id)
        if voice is None or len(voice.references) != 1:
            raise ValueError("A routed candidate must resolve to one recording")
        path = voice.references[0]
        payload = read_voice_reference_bytes(voice, path)
        digest = hashlib.sha256(payload).hexdigest()
        if candidate.reference_sha256s != (digest,) or digest not in by_digest:
            raise ValueError(f"Reference changed during analysis: {path}")
        variant = by_voice.get(voice.character, by_digest[digest])
        if variant["reference_sha256"] != digest:
            raise ValueError(f"Catalog checksum mismatch: {path}")
        quality_score = variant["quality_score"]
        if not isinstance(quality_score, int):
            raise ValueError("Invalid catalog quality score")
        relative = str(path.relative_to(manifest.parent))
        try:
            quality = analyze_reference_bytes(payload, path=path)
        except ValueError as error:
            excluded.append(
                {"path": relative, "sha256": digest, "reasons": [str(error)]}
            )
            continue
        excerpts = variant.get("source_excerpts")
        record = Clip(
            path=relative,
            sha256=digest,
            source_bank=candidate.source_bank,
            source_line_ids=candidate.source_line_ids,
            quality_score=quality_score,
            seconds=quality["duration_seconds"],
            text=" ".join(
                str(excerpt["text"])
                for excerpt in (excerpts if isinstance(excerpts, list) else ())
                if isinstance(excerpt, dict)
            ),
        )
        if quality["rejection_reasons"]:
            excluded.append({**record, "reasons": quality["rejection_reasons"]})
        elif digest in holdout_digests:
            held_out.append(unit_embedding(embed, payload))
        else:
            pending.append((record, path))

    if sha256_file(manifest) != manifest_digest:
        raise ValueError("The candidate manifest changed during analysis")
    if holdout_bank and not held_out:
        raise ValueError("No passing originals in the requested held-out bank")
    for record, path in pending:
        if check_cancelled is not None:
            check_cancelled()
        # A small fresh catalog keeps its safe baseline without loading a model.
        if len(pending) < 3 and not holdout_bank:
            fit.append((record, None))
            continue
        payload = read_regular_bytes(path)
        if hashlib.sha256(payload).hexdigest() != record["sha256"]:
            raise ValueError(f"Reference changed during analysis: {path}")
        try:
            vector = unit_embedding(embed, payload)
        except ValueError as error:
            excluded.append({**record, "reasons": [str(error)]})
            continue
        fit.append((record, vector))
    if holdout_bank and len(fit) < 3:
        raise ValueError("Choosing a representative requires at least three originals")
    scores = (
        typicality_scores([vector for _, vector in fit if vector is not None])
        if len(fit) >= 3
        else None
    )
    # Break ties in existing planner order. Only equally qualified clips compete.
    winner = 0
    if scores is not None:
        best_quality = fit[0][0]["quality_score"]
        winner = max(
            (
                i
                for i, (row, _) in enumerate(fit)
                if row["quality_score"] == best_quality
            ),
            key=lambda i: float(scores[i]),
        )
    anchor_mean = np.mean(np.stack(held_out), axis=0) if held_out else None

    def score_original(index: int) -> ScoredClip:
        row, vector = fit[index]
        return {
            **row,
            "mean_peer_cosine": float(scores[index]) if scores is not None else None,
            "mean_holdout_cosine": float(anchor_mean @ vector)
            if anchor_mean is not None and vector is not None
            else None,
        }

    rendered: list[dict[str, object]] = []
    if anchor_mean is not None:
        for path in renders:
            payload = read_regular_bytes(path)
            rendered.append(
                {
                    "path": str(path),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "mean_holdout_cosine": float(
                        anchor_mean @ unit_embedding(embed, payload)
                    ),
                }
            )
    return {
        "manifest_sha256": manifest_digest,
        "character": character,
        "encoder_revision": REVISION,
        "holdout_bank": holdout_bank,
        "fit_count": len(fit),
        "holdout_count": len(held_out),
        "excluded_preflight": excluded,
        "baseline": score_original(0) if fit else None,
        "representative": score_original(winner) if fit else None,
        "originals_by_typicality": [
            score_original(int(i))
            for i in (
                np.argsort(-scores, stable=True)
                if scores is not None
                else range(len(fit))
            )
        ],
        "renders": rendered,
    }


def delivery_features(payload: bytes) -> dict[str, float] | None:
    """Frozen Praat measurements from the delivery study, on verified audio bytes."""
    import parselmouth
    import soundfile as sf

    audio, sample_rate = sf.read(io.BytesIO(payload), always_2d=True)
    audio = audio.mean(axis=1)
    pitch = parselmouth.Sound(audio, sampling_frequency=sample_rate).to_pitch(
        time_step=0.01, pitch_floor=65, pitch_ceiling=650
    )
    frequencies = pitch.selected_array["frequency"]
    voiced = frequencies > 0
    frequencies = frequencies[voiced]
    if len(frequencies) < 50:
        return None
    centres = np.rint(sample_rate * pitch.xs()[voiced]).astype(int)
    half_window = round(0.0125 * sample_rate)
    levels = np.array(
        [
            10
            * np.log10(
                max(
                    np.mean(audio[max(0, c - half_window) : c + half_window] ** 2),
                    1e-12,
                )
            )
            for c in centres
        ]
    )
    return {
        "pitch_hz": float(np.median(frequencies)),
        "pitch_span_st": float(
            np.ptp(np.percentile(12 * np.log2(frequencies), [10, 90]))
        ),
        "level_span_db": float(np.ptp(np.percentile(levels, [10, 90]))),
    }


def steady_delivery_pool(
    manifest: Path,
    report: Mapping[str, Any],
    feature_extractor: Callable[[bytes], dict[str, float] | None] = delivery_features,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Measure and rank every eligible original before the punctuation gate."""
    pool: list[dict[str, Any]] = []
    excluded = []
    if report["fit_count"] >= 3:
        for row in report["originals_by_typicality"]:
            if check_cancelled is not None:
                check_cancelled()
            if row["quality_score"] != 100:
                continue
            path = manifest.parent / row["path"]
            payload = read_regular_bytes(path)
            if hashlib.sha256(payload).hexdigest() != row["sha256"]:
                raise ValueError(f"Reference changed during selection: {path}")
            try:
                features = feature_extractor(payload)
            except ValueError as error:
                excluded.append({"sha256": row["sha256"], "reason": str(error)})
                continue
            if features is None or not all(
                np.isfinite(value) for value in features.values()
            ):
                excluded.append(
                    {"sha256": row["sha256"], "reason": "insufficient_voiced_frames"}
                )
                continue
            pool.append({**row, **features})
    # Midpoint percentiles reproduce (rankdata(values) - .5) / count, including ties.
    for field in ("pitch_hz", "pitch_span_st", "level_span_db", "mean_peer_cosine"):
        values = np.array([row[field] for row in pool])
        order = np.argsort(values, stable=True)
        start = 0
        while start < len(order):
            end = start + 1
            while end < len(order) and values[order[end]] == values[order[start]]:
                end += 1
            rank = (start + end) / (2 * len(order))
            for index in order[start:end]:
                pool[index][field + "_rank"] = rank
            start = end
    return pool, excluded


def steady_acoustics_pass(row: Mapping[str, float]) -> bool:
    return (
        row["mean_peer_cosine_rank"] >= 0.75
        and 0.25 <= row["pitch_hz_rank"] <= 0.75
        and 0.10 <= row["pitch_span_st_rank"] <= 0.50
        and 0.10 <= row["level_span_db_rank"] <= 0.50
    )


def steady_text_pass(text: str) -> bool:
    return bool(text.strip()) and not any(
        mark in text for mark in ("?", "!", "...", "…", "*")
    )


def select_reference(
    manifest: Path,
    character: str,
    embed: Encoder,
    *,
    speech_backend: str = "moss-tts",
    feature_extractor: Callable[[bytes], dict[str, float] | None] = delivery_features,
    holdout_bank: str | None = None,
    check_cancelled: Callable[[], None] | None = None,
    _catalog: Catalog | None = None,
) -> SelectionReport:
    """Run the frozen selector, optionally reproducing a held-out listening trial."""
    started = time.perf_counter()
    embedding_seconds = 0.0

    def timed_embed(payload: bytes) -> object:
        nonlocal embedding_seconds
        before = time.perf_counter()
        try:
            return embed(payload)
        finally:
            embedding_seconds += time.perf_counter() - before

    report = study(
        manifest,
        character,
        holdout_bank,
        timed_embed,
        speech_backend=speech_backend,
        check_cancelled=check_cancelled,
        _catalog=_catalog,
    )
    selected: dict[str, Any] | None = (
        dict(report["representative"]) if report["representative"] is not None else None
    )
    method = "typical" if report["fit_count"] >= 3 else "baseline"
    reason = "no_steady_candidate" if method == "typical" else "insufficient_originals"
    feature_started = time.perf_counter()
    pool, excluded = steady_delivery_pool(
        manifest, report, feature_extractor, check_cancelled=check_cancelled
    )
    options = [
        row
        for row in pool
        if steady_acoustics_pass(row) and steady_text_pass(row["text"])
    ]
    if options:
        selected = options[0]  # Already ordered by peer cosine, then planner order.
        method, reason = "steady", "frozen_criteria_passed"
    elif selected is None:
        method, reason = "abstain", "no_eligible_originals"
    if sha256_file(manifest) != report["manifest_sha256"]:
        raise ValueError("The candidate manifest changed during selection")
    return {
        **report,
        "selected": selected,
        "selection_method": method,
        "selection_reason": reason,
        "steady_pool_count": len(pool),
        "steady_candidates": options,
        "excluded_features": excluded,
        "timing_seconds": {
            "selection_total": time.perf_counter() - started,
            "embedding": embedding_seconds,
            "delivery_features": time.perf_counter() - feature_started,
        },
    }


def make_reference_selector(
    manifest: Path,
    *,
    speech_backend: str,
    expected_manifest_sha256: str | None = None,
    check_cancelled: Callable[[], None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> Callable[[str], ReferenceRanking]:
    """Rank each full character pool once, sharing one lazily loaded CPU encoder.

    Returns the preferred and Typical checksums. Small pools use their passing
    baseline without loading a model. Larger pools install the pinned CPU model
    on first use, then reuse the verified installation.
    """
    manifest = manifest.parent.resolve() / manifest.name
    expected = expected_manifest_sha256 or sha256_file(manifest)
    catalog: Catalog | None = None
    encoder: Encoder | None = None
    cache: dict[
        str,
        tuple[ReferenceRanking, tuple[tuple[Path, str], ...]],
    ] = {}

    def assert_manifest() -> None:
        if check_cancelled is not None:
            check_cancelled()
        if sha256_file(manifest) != expected:
            raise ValueError("The candidate manifest changed during selection")

    def embed(payload: bytes) -> object:
        nonlocal encoder
        if encoder is None:
            try:
                available = (
                    find_spec("speechbrain") is not None
                    and version("speechbrain") == RUNTIME_VERSION
                )
            except PackageNotFoundError:
                available = False
            if not available:
                raise ReferenceSelectionUnavailable(
                    "The reference-selection runtime is missing or incompatible. "
                    "Repair the application installation and retry."
                )
            if find_spec("parselmouth") is None:
                raise ReferenceSelectionUnavailable(
                    "The reference-selection pitch runtime is missing. "
                    "Repair the application installation and retry."
                )
            installation = managed_speaker_identity_installation()
            if not installation.exists() and not installation.is_symlink():
                message = (
                    "Preparing the reference-selection model "
                    "(about 89 MB on first use)..."
                )
                logger.info(message)
                if progress is not None:
                    progress(message)
                assert_manifest()
                try:
                    install_managed_speaker_identity_model()
                except SpeakerIdentityModelError as error:
                    raise ReferenceSelectionUnavailable(
                        "Unable to prepare the reference-selection model. "
                        "Check the network connection and available disk space, "
                        f"then retry: {error}"
                    ) from error
                assert_manifest()
            # The resolver verifies the pinned files and historical provenance.
            directory = resolve_managed_speaker_identity_model()
            try:
                encoder = make_speechbrain_embedder(directory)
            except ImportError as error:
                raise ReferenceSelectionUnavailable(
                    "The reference-selection runtime is incomplete. "
                    "Repair the application installation and retry."
                ) from error
        return encoder(payload)

    def features(payload: bytes) -> dict[str, float] | None:
        try:
            return delivery_features(payload)
        except ImportError as error:
            raise ReferenceSelectionUnavailable(
                "The reference-selection pitch runtime is incomplete. "
                "Repair the application installation and retry."
            ) from error

    def select(character: str) -> ReferenceRanking:
        nonlocal catalog
        assert_manifest()
        key = normalize_character_name(character)
        if key in cache:
            result, references = cache[key]
            for path, digest in references:
                if check_cancelled is not None:
                    check_cancelled()
                if hashlib.sha256(read_regular_bytes(path)).hexdigest() != digest:
                    raise ValueError(f"Reference changed during selection: {path}")
            assert_manifest()
            return result
        if catalog is None:
            catalog = validated_player_voice_candidates(
                manifest, speech_backend=speech_backend
            )
            assert_manifest()
        report = select_reference(
            manifest,
            character,
            embed,
            speech_backend=speech_backend,
            feature_extractor=features,
            check_cancelled=check_cancelled,
            _catalog=catalog,
        )
        assert_manifest()
        preferred, typical = report["selected"], report["representative"]
        result = ReferenceRanking(
            preferred=str(preferred["sha256"]) if preferred is not None else None,
            typical=str(typical["sha256"]) if typical is not None else None,
            excluded=frozenset(
                str(row["sha256"]) for row in report["excluded_preflight"]
            ),
        )
        references = tuple(
            (manifest.parent / str(row["path"]), str(row["sha256"]))
            for row in (
                *report["originals_by_typicality"],
                *report["excluded_preflight"],
            )
        )
        cache[key] = result, references
        return result

    return select


def catalog_characters(
    manifest: Path, *, speech_backend: str = "moss-tts"
) -> list[dict[str, object]]:
    """List routed candidates without loading speaker or pitch runtimes."""
    _registry, variants = validated_player_voice_candidates(
        manifest, speech_backend=speech_backend
    )
    characters: dict[str, set[str]] = {}
    for row in variants:
        origin = row.get("candidate_origin")
        duration = row.get("duration_seconds")
        character = row.get("character")
        digest = row.get("reference_sha256")
        if not isinstance(character, str) or not isinstance(digest, str):
            continue
        if automatic_voice_reference_is_eligible(
            candidate_origin=origin if isinstance(origin, str) else None,
            source_bank=row.get("source_bank"),
            source_line_ids=row.get("source_line_ids", ()),
            duration_seconds=float(duration)
            if isinstance(duration, (int, float))
            else None,
        ):
            characters.setdefault(character, set()).add(digest)
    return [
        {"character": character, "eligible_catalog_clips": len(digests)}
        for character, digests in sorted(
            characters.items(), key=lambda row: row[0].casefold()
        )
    ]

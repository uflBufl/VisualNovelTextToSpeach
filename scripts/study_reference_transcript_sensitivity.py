"""Replay frozen steady selection using checksum-bound source ASR transcripts.

Run ``python -m scripts.study_reference_transcript_sensitivity MANIFEST REPORT
TRANSCRIPTS``. REPORT is the original study/selection report, containing every
``originals_by_typicality`` row. Historical rows without text use the verified
catalog transcript; supplied text must match it. TRANSCRIPTS contains ``{"transcripts":
[{"sha256": "...", "text": "Whisper transcript"}]}``; extra provenance is retained.
Use ``--candidates-only`` without TRANSCRIPTS to list all acoustically qualified
sources, including those excluded by catalog punctuation. No models are loaded
except the existing optional delivery-feature runtime.
Use ``--transcribe-sources`` without TRANSCRIPTS to produce the cache with the
pinned, already installed managed Whisper model; ``--asr-model-root`` overrides
its managed installation root. Source channels are averaged into PCM16 WAV.

The sensitivity arm uses ASR punctuation and vetoes lexical discrepancies with
at least three token edits AND an edit fraction of at least 0.25. Edits are the
sum of maximum span lengths in non-equal SequenceMatcher opcodes, divided by the
longer normalized transcript. Isolated name substitutions cannot meet this rule.
Missing or empty ASR makes coverage unknown and preserves the typical fallback.
These are research sensitivity rules, not a claim of transcription correctness.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from collections.abc import Callable
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, cast

from vntts.cli import cli_error, print_json
from vntts.document_identity import sha256_file
from vntts.path_safety import read_regular_bytes
from vntts.pregeneration_voices import (
    STORY_LINE_ROUTE,
    validated_player_voice_candidates,
)
from vntts.reference_selection import (
    delivery_features,
    steady_acoustics_pass,
    steady_delivery_pool,
    steady_text_pass,
)
from vntts.speech_content_advice import normalize_speech_text
from vntts.voices import normalize_character_name


def transcript_discrepancy(expected: str, observed: str | None) -> dict[str, Any]:
    left = normalize_speech_text(expected)
    right = normalize_speech_text(observed or "")
    if not left or not right:
        return {"status": "unknown", "token_edits": None, "edit_fraction": None}
    edits = sum(
        max(end - start, other_end - other_start)
        for tag, start, end, other_start, other_end in SequenceMatcher(
            None, left, right, autojunk=False
        ).get_opcodes()
        if tag != "equal"
    )
    fraction = edits / max(len(left), len(right))
    return {
        "status": "material"
        if edits >= 3 and fraction >= 0.25
        else "lexical_match"
        if not edits
        else "minor_or_uncertain",
        "token_edits": edits,
        "edit_fraction": fraction,
    }


def acoustic_candidates(
    manifest: Path,
    report: dict[str, Any],
    *,
    feature_extractor: Callable[[bytes], dict[str, float] | None] = delivery_features,
) -> dict[str, Any]:
    """Verify original sources and reuse the selector's unchanged acoustic gates."""
    if sha256_file(manifest) != report["manifest_sha256"]:
        raise ValueError("The candidate manifest changed since the study")
    rows = report["originals_by_typicality"]
    count = report["fit_count"]
    if (
        not isinstance(rows, list)
        or type(count) is not int
        or count < 0
        or len(rows) != count
    ):
        raise ValueError("The study report must contain every fitted original")
    root = manifest.parent.resolve()
    # Reject unbound paths before reading any report-supplied file.
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise ValueError("Malformed original row")
        relative = Path(row["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("Original path must be relative to its manifest")
        if not (root / relative).resolve().is_relative_to(root):
            raise ValueError("Original path escapes its manifest")
    registry, variants = validated_player_voice_candidates(
        manifest, speech_backend="moss-tts"
    )
    catalog = {}
    for variant in variants:
        if variant["candidate_origin"] != STORY_LINE_ROUTE or normalize_character_name(
            variant["character"]
        ) != normalize_character_name(report["character"]):
            continue
        voice = registry.resolve_source(
            f"character:{normalize_character_name(variant['voice_character'])}"
        )
        if voice is not None and len(voice.references) == 1:
            catalog[
                (
                    voice.references[0].relative_to(root).as_posix(),
                    variant["reference_sha256"],
                )
            ] = variant
    seen = set()
    canonical_rows = []
    for row in rows:
        path = manifest.parent / row["path"]
        source_variant = catalog.get((Path(row["path"]).as_posix(), row.get("sha256")))
        if source_variant is None:
            raise ValueError("Original source is not bound to this character's catalog")
        excerpts = cast(list[dict[str, object]], source_variant["source_excerpts"])
        text = " ".join(str(excerpt["text"]) for excerpt in excerpts)
        if (
            ("text" in row and row["text"] != text)
            or row.get("quality_score") != source_variant["quality_score"]
            or row.get("source_bank") != source_variant.get("source_bank")
            or list(row.get("source_line_ids", ())) != source_variant["source_line_ids"]
        ):
            raise ValueError("Original metadata differs from its catalog variant")
        cosine = row.get("mean_peer_cosine")
        if count >= 3 and (type(cosine) not in (int, float) or not -1 <= cosine <= 1):
            raise ValueError("Original peer cosine must be finite and within [-1, 1]")
        if row["sha256"] in seen:
            raise ValueError("Duplicate original checksum in study report")
        seen.add(row["sha256"])
        if hashlib.sha256(read_regular_bytes(path)).hexdigest() != row["sha256"]:
            raise ValueError(f"Reference changed since the study: {path}")
        canonical_rows.append({**row, "text": text})
    fallback = report["representative"]
    if fallback is not None:
        if not isinstance(fallback, dict):
            raise ValueError("Typical fallback must be one of the fitted originals")
        canonical_fallback = next(
            (
                row
                for row in canonical_rows
                if row == {**fallback, "text": fallback.get("text", row["text"])}
            ),
            None,
        )
        if canonical_fallback is None:
            raise ValueError("Typical fallback must be one of the fitted originals")
        fallback = canonical_fallback
    if rows and fallback is None:
        raise ValueError("A nonempty study requires its typical fallback")
    pool, excluded = steady_delivery_pool(
        manifest,
        {**report, "originals_by_typicality": canonical_rows},
        feature_extractor,
    )
    if sha256_file(manifest) != report["manifest_sha256"]:
        raise ValueError("The candidate manifest changed during replay")
    return {
        "character": report["character"],
        "manifest_sha256": report["manifest_sha256"],
        "representative": fallback,
        "steady_pool_count": len(pool),
        "acoustic_candidates": [row for row in pool if steady_acoustics_pass(row)],
        "excluded_features": excluded,
    }


def replay_transcript_sensitivity(
    manifest: Path,
    report: dict[str, Any],
    transcripts: dict[str, Any],
    *,
    feature_extractor: Callable[[bytes], dict[str, float] | None] = delivery_features,
) -> dict[str, Any]:
    result = acoustic_candidates(manifest, report, feature_extractor=feature_extractor)
    by_digest = {}
    for row in transcripts["transcripts"]:
        digest, text = row["sha256"], row["text"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Transcript cache requires SHA-256 source checksums")
        if digest in by_digest:
            raise ValueError("Duplicate transcript source checksum")
        if not isinstance(text, str):
            raise ValueError("Transcript text must be a string")
        by_digest[digest] = text
    candidates = []
    for row in result["acoustic_candidates"]:
        transcript = by_digest.get(row["sha256"])
        candidates.append(
            {
                **row,
                "asr_text": transcript,
                "catalog_punctuation_pass": steady_text_pass(row["text"]),
                "asr_punctuation_pass": steady_text_pass(transcript or ""),
                "discrepancy": transcript_discrepancy(row["text"], transcript),
            }
        )
    unknown = [
        row["sha256"] for row in candidates if row["discrepancy"]["status"] == "unknown"
    ]
    decisions = {}
    for arm in ("catalog", "asr", "sensitivity"):
        eligible = [
            row
            for row in candidates
            if row[f"{'catalog' if arm == 'catalog' else 'asr'}_punctuation_pass"]
            and (arm != "sensitivity" or row["discrepancy"]["status"] != "material")
        ]
        coverage_unknown = arm != "catalog" and bool(unknown)
        selected = None if coverage_unknown or not eligible else eligible[0]
        method = "steady"
        reason = "frozen_criteria_passed"
        if selected is None:
            selected = result["representative"]
            method = "typical" if report["fit_count"] >= 3 else "baseline"
            reason = (
                "transcript_coverage_unknown"
                if coverage_unknown
                else "no_steady_candidate"
            )
            if selected is None:
                method, reason = "abstain", "no_eligible_originals"
        decisions[arm] = {
            "selected": selected,
            "selection_method": method,
            "selection_reason": reason,
            "eligible_sha256s": [row["sha256"] for row in eligible],
        }
    return {
        **result,
        "acoustic_candidates": candidates,
        "unknown_transcript_sha256s": unknown,
        "transcript_provenance": {
            k: v for k, v in transcripts.items() if k != "transcripts"
        },
        "discrepancy_rule": {"minimum_token_edits": 3, "minimum_edit_fraction": 0.25},
        "decisions": decisions,
    }


def transcribe_sources(
    manifest: Path,
    report: dict[str, Any],
    *,
    transcriber: Callable[[bytes], str] | None = None,
    asr_model_root: Path | None = None,
    feature_extractor: Callable[[bytes], dict[str, float] | None] = delivery_features,
) -> dict[str, Any]:
    """Acquire checksum-bound source transcripts without downloading a model."""
    import soundfile as sf

    from vntts.authoring.asr_model import WHISPER_TINY_EN, resolve_managed_asr_model

    sources = acoustic_candidates(manifest, report, feature_extractor=feature_extractor)
    if sources["acoustic_candidates"] and transcriber is None:
        from vntts.preview_content_advisor import _whisper_transcriber

        transcriber = _whisper_transcriber(
            resolve_managed_asr_model(root=asr_model_root)
        )
    transcripts = []
    for row in sources["acoustic_candidates"]:
        assert transcriber is not None
        payload = read_regular_bytes(manifest.parent / row["path"])
        if hashlib.sha256(payload).hexdigest() != row["sha256"]:
            raise ValueError("Reference changed during transcription")
        audio, rate = sf.read(io.BytesIO(payload), always_2d=True)
        mono = io.BytesIO()
        sf.write(mono, audio.mean(axis=1), rate, format="WAV", subtype="PCM_16")
        converted = mono.getvalue()
        text = transcriber(converted)
        if not isinstance(text, str):
            raise ValueError("Source transcriber must return text")
        transcripts.append(
            {
                "sha256": row["sha256"],
                "asr_input_sha256": hashlib.sha256(converted).hexdigest(),
                "text": text,
            }
        )
    if sha256_file(manifest) != report["manifest_sha256"]:
        raise ValueError("The candidate manifest changed during transcription")
    return {
        "character": report["character"],
        "manifest_sha256": report["manifest_sha256"],
        "analyzer": {
            "model_id": WHISPER_TINY_EN.model_id,
            "repository": WHISPER_TINY_EN.repository,
            "revision": WHISPER_TINY_EN.revision,
            "tree_sha256": WHISPER_TINY_EN.tree_sha256,
        },
        "input_conversion": "arithmetic channel mean; PCM16 WAV at source sample rate",
        "transcripts": transcripts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("transcripts", type=Path, nargs="?")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--candidates-only", action="store_true")
    actions.add_argument("--transcribe-sources", action="store_true")
    parser.add_argument("--asr-model-root", type=Path)
    args = parser.parse_args()
    if (args.candidates_only or args.transcribe_sources) == (
        args.transcripts is not None
    ):
        parser.error(
            "Supply TRANSCRIPTS or use --candidates-only / --transcribe-sources"
        )
    try:
        report_payload = read_regular_bytes(args.report)
        report = json.loads(report_payload)
        if args.candidates_only:
            result = acoustic_candidates(args.manifest, report)
        elif args.transcribe_sources:
            result = transcribe_sources(
                args.manifest, report, asr_model_root=args.asr_model_root
            )
        else:
            transcript_payload = read_regular_bytes(args.transcripts)
            result = replay_transcript_sensitivity(
                args.manifest, report, json.loads(transcript_payload)
            )
            result["transcript_cache_sha256"] = hashlib.sha256(
                transcript_payload
            ).hexdigest()
        result["study_report_sha256"] = hashlib.sha256(report_payload).hexdigest()
    except (
        ImportError,
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
    ) as error:
        return cli_error(error)
    print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

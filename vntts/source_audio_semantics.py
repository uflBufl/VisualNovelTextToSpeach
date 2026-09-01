"""Consumer-side validation for checksum-bound source-audio semantic evidence."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document

from vntts.document_identity import canonical_document_sha256

SEMANTIC_EVIDENCE_SCHEMA = "r1999.source-audio-semantic-evidence"
SEMANTIC_EVIDENCE_VERSION = 1
SEMANTIC_EVIDENCE_METHOD = "local-asr-exact-normalized-transcript"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
WORD_PATTERN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", flags=re.UNICODE)


class SourceAudioSemanticEvidenceError(RuntimeError):
    """Semantic source-audio evidence is missing, stale or malformed."""


def normalize_semantic_text(text):
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return " ".join(
        token.casefold().replace("’", "'") for token in WORD_PATTERN.findall(normalized)
    )


def semantic_text_sha256(text):
    return hashlib.sha256(normalize_semantic_text(text).encode("utf-8")).hexdigest()


def load_source_audio_semantic_evidence(path, story_index_path=None):
    evidence_path = Path(path).expanduser().resolve()
    try:
        document = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SourceAudioSemanticEvidenceError(
            f"Unable to read source-audio semantic evidence {evidence_path}: {error}"
        ) from error
    validate_source_audio_semantic_evidence(document)
    if story_index_path is not None:
        validate_story_semantic_evidence(story_index_path, evidence_path, document)
    return document


def validate_source_audio_semantic_evidence(document):
    if (
        not isinstance(document, dict)
        or document.get("schema") != SEMANTIC_EVIDENCE_SCHEMA
        or document.get("schema_version") != SEMANTIC_EVIDENCE_VERSION
    ):
        raise SourceAudioSemanticEvidenceError(
            "Unsupported source-audio semantic evidence schema"
        )
    locale = document.get("locale")
    model = document.get("model")
    entries = document.get("entries")
    if not isinstance(locale, str) or not locale.strip():
        raise SourceAudioSemanticEvidenceError("Semantic evidence locale is invalid")
    if (
        not isinstance(model, dict)
        or model.get("kind") != "whisper"
        or model.get("decoding") != "deterministic_greedy_default"
    ):
        raise SourceAudioSemanticEvidenceError("Semantic evidence model is invalid")
    model_sha256 = _require_sha256(model.get("sha256"), "semantic evidence model")
    _require_sha256(
        document.get("source_story_index_sha256"),
        "semantic evidence source story index",
    )
    if not isinstance(entries, list) or not entries:
        raise SourceAudioSemanticEvidenceError("Semantic evidence entries are empty")
    keys = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("locale") != locale:
            raise SourceAudioSemanticEvidenceError(
                "Semantic evidence entry locale changed"
            )
        media_sha256 = _require_sha256(entry.get("media_sha256"), "semantic media")
        normalized_text_sha256 = _require_sha256(
            entry.get("normalized_displayed_text_sha256"),
            "semantic normalized displayed text",
        )
        _require_sha256(entry.get("displayed_text_sha256"), "semantic displayed text")
        if entry.get("model_sha256") != model_sha256:
            raise SourceAudioSemanticEvidenceError(
                "Semantic entry model binding changed"
            )
        observed = entry.get("observed_transcript")
        if not isinstance(observed, str) or not normalize_semantic_text(observed):
            raise SourceAudioSemanticEvidenceError("Semantic transcript is empty")
        if entry.get("normalized_observed_text_sha256") != semantic_text_sha256(
            observed
        ):
            raise SourceAudioSemanticEvidenceError("Semantic transcript hash changed")
        verdict = entry.get("verdict")
        expected_reason = (
            "exact-normalized-asr-transcript"
            if verdict == "full"
            else "asr-transcript-mismatch"
        )
        if verdict not in {"full", "partial"} or entry.get("reason") != expected_reason:
            raise SourceAudioSemanticEvidenceError("Semantic verdict is invalid")
        if entry.get("method") != SEMANTIC_EVIDENCE_METHOD:
            raise SourceAudioSemanticEvidenceError("Semantic evidence method changed")
        source_line_ids = entry.get("source_line_ids")
        if (
            not isinstance(source_line_ids, list)
            or source_line_ids != sorted(set(source_line_ids))
            or any(
                not isinstance(line_id, str) or not line_id
                for line_id in source_line_ids
            )
        ):
            raise SourceAudioSemanticEvidenceError(
                "Semantic source line IDs are invalid"
            )
        expected_entry_id = canonical_document_sha256(
            {
                key: value
                for key, value in entry.items()
                if key not in {"entry_id", "source_line_ids"}
            }
        )
        if entry.get("entry_id") != expected_entry_id:
            raise SourceAudioSemanticEvidenceError("Semantic evidence entry ID changed")
        keys.append((locale, media_sha256, normalized_text_sha256))
    if keys != sorted(set(keys)):
        raise SourceAudioSemanticEvidenceError(
            "Semantic evidence entries are duplicated or not canonical"
        )
    authority = {
        key: value
        for key, value in document.items()
        if key not in {"evidence_id", "generated_at"}
    }
    if document.get("evidence_id") != canonical_document_sha256(authority):
        raise SourceAudioSemanticEvidenceError("Semantic evidence ID changed")
    return document


def validate_story_semantic_evidence(story_index_path, evidence_path, evidence):
    try:
        story = load_story_index_document(story_index_path)
    except StoryIndexError as error:
        raise SourceAudioSemanticEvidenceError(str(error)) from error
    metadata = story.metadata.get("source_audio_semantics")
    if not isinstance(metadata, dict):
        raise SourceAudioSemanticEvidenceError(
            "Story index has no source-audio semantic evidence binding"
        )
    if (
        metadata.get("evidence_id") != evidence["evidence_id"]
        or metadata.get("evidence_sha256") != sha256_file(evidence_path)
        or metadata.get("method") != SEMANTIC_EVIDENCE_METHOD
    ):
        raise SourceAudioSemanticEvidenceError(
            "Story source-audio semantic evidence binding changed"
        )
    entry_by_id = {entry["entry_id"]: entry for entry in evidence["entries"]}
    matched = 0
    for line in story.records:
        record = line.to_record()
        entry_id = record.get("source_audio_semantic_evidence_entry_id")
        if entry_id is None:
            continue
        entry = entry_by_id.get(entry_id)
        if (
            record.get("source_audio_semantic_evidence_id") != evidence["evidence_id"]
            or entry is None
            or entry["locale"] != story.metadata.get("language")
            or entry["media_sha256"] != record.get("source_audio_duration_media_sha256")
            or entry["displayed_text_sha256"] != record.get("text_sha256")
            or entry["normalized_displayed_text_sha256"]
            != semantic_text_sha256(record.get("text"))
            or entry["verdict"] != record.get("source_audio_completeness")
            or entry["reason"] != record.get("source_audio_completeness_reason")
        ):
            raise SourceAudioSemanticEvidenceError(
                f"Story semantic evidence changed for {record.get('line_id')!r}"
            )
        matched += 1
    if matched != metadata.get("applied_count") or matched <= 0:
        raise SourceAudioSemanticEvidenceError(
            "Story semantic evidence applied count changed"
        )
    return evidence


def _require_sha256(value, label):
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise SourceAudioSemanticEvidenceError(f"{label} SHA-256 is invalid")
    return value

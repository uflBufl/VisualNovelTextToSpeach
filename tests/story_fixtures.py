"""Shared story inputs and their synthetic source-audio semantic evidence."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import write_story_index_document

from vntts.document_identity import canonical_document_sha256
from vntts.source_audio_semantics import SEMANTIC_EVIDENCE_METHOD, semantic_text_sha256


@dataclass(frozen=True)
class PublishedSemanticEvidence:
    evidence_id: str
    entry_id: str
    sha256: str


def semantic_evidence_entry(
    *,
    line_id: str,
    text: str,
    displayed_text_sha256: str,
    media_id: int | None,
    media_sha256: str = "a" * 64,
    model_sha256: str = "b" * 64,
) -> tuple[dict[str, object], str]:
    entry: dict[str, object] = {
        "locale": "en",
        **({"media_id": media_id} if media_id is not None else {}),
        "media_sha256": media_sha256,
        "displayed_text_sha256": displayed_text_sha256,
        "normalized_displayed_text_sha256": semantic_text_sha256(text),
        "observed_transcript": text,
        "normalized_observed_text_sha256": semantic_text_sha256(text),
        "verdict": "full",
        "reason": "exact-normalized-asr-transcript",
        "method": SEMANTIC_EVIDENCE_METHOD,
        "model_sha256": model_sha256,
        "source_line_ids": [line_id],
    }
    entry_id = canonical_document_sha256(
        {key: value for key, value in entry.items() if key != "source_line_ids"}
    )
    entry["entry_id"] = entry_id
    return entry, entry_id


def semantic_evidence_document(
    entries: list[dict[str, object]],
    *,
    model_sha256: str = "b" * 64,
    source_story_index_sha256: str = "c" * 64,
    snapshot: str = "synthetic",
    generated_at: str = "2026-09-14T00:00:00+00:00",
) -> tuple[dict[str, object], str]:
    evidence: dict[str, object] = {
        "schema": "r1999.source-audio-semantic-evidence",
        "schema_version": 1,
        "locale": "en",
        "source_story_index_sha256": source_story_index_sha256,
        "model": {
            "kind": "whisper",
            "snapshot": snapshot,
            "sha256": model_sha256,
            "device": "cpu",
            "decoding": "deterministic_greedy_default",
        },
        "entries": entries,
    }
    evidence_id = canonical_document_sha256(evidence)
    evidence["evidence_id"] = evidence_id
    evidence["generated_at"] = generated_at
    return evidence, evidence_id


def write_source_audio_semantic_evidence(
    path: Path,
    *,
    line_id: str,
    text: str,
    media_id: int | None,
    media_sha256: str = "a" * 64,
    model_sha256: str = "b" * 64,
    source_story_index_sha256: str = "c" * 64,
    snapshot: str = "synthetic",
    generated_at: str = "2026-09-14T00:00:00+00:00",
) -> PublishedSemanticEvidence:
    entry, entry_id = semantic_evidence_entry(
        line_id=line_id,
        text=text,
        displayed_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        media_id=media_id,
        media_sha256=media_sha256,
        model_sha256=model_sha256,
    )
    evidence, evidence_id = semantic_evidence_document(
        [entry],
        model_sha256=model_sha256,
        source_story_index_sha256=source_story_index_sha256,
        snapshot=snapshot,
        generated_at=generated_at,
    )
    atomic_write_json(path, evidence, sort_keys=True)
    return PublishedSemanticEvidence(evidence_id, entry_id, sha256_file(path))


def write_verified_source_story(path: Path) -> None:
    line_id = "test:0"
    text = "These old ones are enough to carry everyone."
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    media_hash = "a" * 64
    evidence = write_source_audio_semantic_evidence(
        path.parent / "source-audio-semantic-evidence.json",
        line_id=line_id,
        text=text,
        media_id=70,
    )
    metadata = {
        "game": "Synthetic",
        "language": "en",
        "source_audio_completion": "verified-media-duration-seconds",
        "source_audio_semantics": {
            "evidence_id": evidence.evidence_id,
            "evidence_sha256": evidence.sha256,
            "method": SEMANTIC_EVIDENCE_METHOD,
            "selected_chapters": ["24006"],
            "applied_count": 1,
        },
    }
    record = {
        "record_type": "line",
        "line_id": line_id,
        "chapter": "24006",
        "sequence": 10,
        "speaker": "Kamuta",
        "text": text,
        "text_sha256": text_hash,
        "kind": "dialogue",
        "source_audio_status": "available",
        "source_audio_id": "voice-7",
        "source_audio_duration_seconds": 1.25,
        "source_audio_duration_media_id": 70,
        "source_audio_duration_media_sha256": media_hash,
        "source_audio_duration_sample_rate": 24000,
        "source_audio_duration_sample_count": 30000,
        "source_audio_duration_decoder": "synthetic",
        "source_media_ids": [70],
        "available_media_ids": [70],
        "source_audio_completeness": "full",
        "source_audio_completeness_reason": "exact-normalized-asr-transcript",
        "source_audio_semantic_evidence_id": evidence.evidence_id,
        "source_audio_semantic_evidence_entry_id": evidence.entry_id,
    }
    write_story_index_document(path, metadata, [record])


def write_story_index(root: Path, *, generated_text: str = "Generate me.") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "story-index.jsonl"
    line_id = "reverse1999:1"
    text = "Original game voice."
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    media_hash = "a" * 64
    evidence = write_source_audio_semantic_evidence(
        root / "source-audio-semantic-evidence.json",
        line_id=line_id,
        text=text,
        media_id=7,
    )
    write_story_index_document(
        path,
        {
            "game": "Reverse: 1999",
            "game_version": "3.7",
            "language": "en",
            "source_audio_completion": "verified-media-duration-seconds",
            "source_audio_semantics": {
                "evidence_id": evidence.evidence_id,
                "evidence_sha256": evidence.sha256,
                "method": SEMANTIC_EVIDENCE_METHOD,
                "selected_chapters": ["1"],
                "applied_count": 1,
            },
            "collections": [
                {
                    "collection_id": "main-1",
                    "title": "Main Story 1",
                    "kind": "main-story",
                    "order": 1,
                },
                {
                    "collection_id": "rhiannon",
                    "title": "Rhiannon",
                    "kind": "character-story",
                    "order": 2,
                },
            ],
        },
        [
            {
                "record_type": "line",
                "line_id": line_id,
                "chapter": "1",
                "sequence": 1,
                "speaker": "Centurion",
                "voice_character": "Centurion",
                "episode_title": "The Storm",
                "text": text,
                "text_sha256": text_hash,
                "kind": "dialogue",
                "collection_id": "main-1",
                "source_audio_status": "available",
                "source_audio_duration_seconds": 1.0,
                "source_audio_duration_media_id": 7,
                "source_audio_duration_media_sha256": media_hash,
                "source_audio_duration_sample_rate": 24000,
                "source_audio_duration_sample_count": 24000,
                "source_audio_duration_decoder": "synthetic",
                "source_media_ids": [7],
                "available_media_ids": [7],
                "source_audio_completeness": "full",
                "source_audio_completeness_reason": ("exact-normalized-asr-transcript"),
                "source_audio_semantic_evidence_id": evidence.evidence_id,
                "source_audio_semantic_evidence_entry_id": evidence.entry_id,
                "speakable": True,
            },
            {
                "record_type": "line",
                "line_id": "reverse1999:2",
                "chapter": "1",
                "sequence": 2,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "text": generated_text,
                "kind": "dialogue",
                "collection_id": "main-1",
                "source_audio_status": "absent",
                "speakable": True,
            },
            {
                "record_type": "line",
                "line_id": "reverse1999:3",
                "chapter": "2",
                "sequence": 1,
                "speaker": "Aderyn",
                "voice_character": "Rhiannon child",
                "episode_title": "The Wandering Child",
                "text": "A child line.",
                "kind": "dialogue",
                "collection_id": "rhiannon",
                "source_audio_status": "absent",
                "speakable": True,
            },
        ],
    )
    return path


def write_content(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "story-index.jsonl"
    line_id = "line:original"
    text = "Already voiced."
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    media_hash = "a" * 64
    evidence = write_source_audio_semantic_evidence(
        root / "source-audio-semantic-evidence.json",
        line_id=line_id,
        text=text,
        media_id=7,
    )
    write_story_index_document(
        path,
        {
            "game": "Reverse: 1999",
            "language": "en",
            "source_audio_completion": "verified-media-duration-seconds",
            "source_audio_semantics": {
                "evidence_id": evidence.evidence_id,
                "evidence_sha256": evidence.sha256,
                "method": SEMANTIC_EVIDENCE_METHOD,
                "selected_chapters": ["1"],
                "applied_count": 1,
            },
            "collections": [
                {
                    "collection_id": "story",
                    "title": "Story",
                    "kind": "character-story",
                    "order": 1,
                }
            ],
        },
        [
            {
                "record_type": "line",
                "line_id": line_id,
                "chapter": "1",
                "sequence": 1,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "text": text,
                "text_sha256": text_hash,
                "kind": "dialogue",
                "collection_id": "story",
                "source_audio_status": "available",
                "source_audio_duration_seconds": 1.0,
                "source_audio_duration_media_id": 7,
                "source_audio_duration_media_sha256": media_hash,
                "source_audio_duration_sample_rate": 24000,
                "source_audio_duration_sample_count": 24000,
                "source_audio_duration_decoder": "synthetic",
                "source_media_ids": [7],
                "available_media_ids": [7],
                "source_audio_completeness": "full",
                "source_audio_completeness_reason": ("exact-normalized-asr-transcript"),
                "source_audio_semantic_evidence_id": evidence.evidence_id,
                "source_audio_semantic_evidence_entry_id": evidence.entry_id,
                "speakable": True,
                "portrait": 10,
                "source_bank": "rhiannon.bnk",
            },
            {
                "record_type": "line",
                "line_id": "line:rhiannon:1",
                "chapter": "1",
                "sequence": 2,
                "speaker": "Aderyn",
                "voice_character": "Rhiannon",
                "text": "This is the most useful preview sentence for my voice.",
                "kind": "dialogue",
                "collection_id": "story",
                "source_audio_status": "absent",
                "speakable": True,
                "portrait": 10,
                "source_bank": "rhiannon.bnk",
            },
            {
                "record_type": "line",
                "line_id": "line:rhiannon:2",
                "chapter": "1",
                "sequence": 3,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "text": "Short.",
                "kind": "dialogue",
                "collection_id": "story",
                "source_audio_status": "absent",
                "speakable": True,
                "portrait": 10,
                "source_bank": "rhiannon.bnk",
            },
            {
                "record_type": "line",
                "line_id": "line:unknown",
                "chapter": "1",
                "sequence": 4,
                "speaker": "Hotelier",
                "voice_character": "Hotelier",
                "text": "A one-off role.",
                "kind": "dialogue",
                "collection_id": "story",
                "source_audio_status": "absent",
                "speakable": True,
                "portrait": 20,
                "source_bank": "hotel.bnk",
            },
            {
                "record_type": "line",
                "line_id": "line:unattributed",
                "chapter": "1",
                "sequence": 5,
                "speaker": "???",
                "voice_character": "Someone",
                "text": "Who am I?",
                "kind": "dialogue",
                "collection_id": "story",
                "source_audio_status": "absent",
                "speakable": True,
            },
        ],
    )
    return path

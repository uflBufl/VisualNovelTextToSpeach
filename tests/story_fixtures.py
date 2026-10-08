"""Shared story inputs and their synthetic source-audio semantic evidence."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import PCM16_MONO_WAV_FORMAT, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.game_pack import write_game_pack
from vntts_artifacts.generated_audio import write_generated_audio_manifest
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.live_sequence import write_live_sequence_plan
from vntts_artifacts.story_index import (
    write_story_index as write_producer_story_index,
)
from vntts_artifacts.story_index import (
    write_story_index_document,
)
from vntts_artifacts.voice_manifest import write_voice_manifest

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


def write_synthetic_game_pack(
    root: Path,
    *,
    include_generated: bool = True,
    include_sequence: bool = False,
    include_semantics: bool = False,
) -> tuple[Path, str, str, str, Path | None]:
    line_id = "synthetic:chapter-1:line-7"
    text = "Keep this exact line intact."
    text_hash = text_sha256(text)
    semantic_evidence = None
    semantic_metadata: dict[str, object] | None = None
    published_evidence: PublishedSemanticEvidence | None = None
    if include_semantics:
        media_sha256 = "1" * 64
        semantic_evidence = root / "source-audio-semantic-evidence.json"
        published_evidence = write_source_audio_semantic_evidence(
            semantic_evidence,
            line_id=line_id,
            text=text,
            media_id=11,
            media_sha256=media_sha256,
            model_sha256="2" * 64,
            source_story_index_sha256="3" * 64,
            generated_at="2026-08-30T00:00:00Z",
        )
        semantic_metadata = {
            "evidence_id": published_evidence.evidence_id,
            "evidence_sha256": published_evidence.sha256,
            "method": SEMANTIC_EVIDENCE_METHOD,
            "selected_chapters": ["chapter-1"],
            "applied_count": 1,
        }

    story = root / "story-index.jsonl"
    story_record: dict[str, object] = {
        "record_type": "line",
        "line_id": line_id,
        "chapter": "chapter-1",
        "sequence": 7,
        "speaker": "Ada",
        "text": text,
        "kind": "dialogue",
        "source_audio_status": "absent",
    }
    if published_evidence is not None:
        story_record.update(
            text_sha256=text_hash,
            source_audio_duration_media_sha256=media_sha256,
            source_audio_completeness="full",
            source_audio_completeness_reason="exact-normalized-asr-transcript",
            source_audio_semantic_evidence_id=published_evidence.evidence_id,
            source_audio_semantic_evidence_entry_id=published_evidence.entry_id,
        )
    write_producer_story_index(
        story,
        {
            "game": "Synthetic Game",
            "language": "en",
            **(
                {"source_audio_semantics": semantic_metadata}
                if semantic_metadata is not None
                else {}
            ),
        },
        [story_record],
    )

    voice_wav = root / "voices" / "ada.wav"
    write_pcm16_wav(voice_wav, np.zeros(240, dtype=np.float32), 24_000)
    voices = root / "voice-manifest.json"
    write_voice_manifest(
        voices,
        {
            "version": 2,
            "voices": [
                {
                    "character": "Ada",
                    "speaker": "ada-v1",
                    "references": ["voices/ada.wav"],
                }
            ],
        },
    )

    generated = None
    generated_wav = None
    if include_generated:
        generated_wav = root / "generated" / "line-7.wav"
        write_pcm16_wav(
            generated_wav,
            np.linspace(-0.1, 0.1, 240, dtype=np.float32),
            24_000,
        )
        generated = root / "generated-audio.json"
        write_generated_audio_manifest(
            generated,
            {"game": "Synthetic Game", "language": "en"},
            [
                {
                    "line_id": line_id,
                    "text_sha256": text_hash,
                    "audio": "generated/line-7.wav",
                    "audio_format": PCM16_MONO_WAV_FORMAT,
                    "audio_sha256": sha256_file(generated_wav),
                    "sample_rate": 24_000,
                    "sample_count": 240,
                }
            ],
        )

    pack_path = root / "game-pack.json"
    components = {"story_index": story, "voice_manifest": voices}
    if generated is not None:
        components["generated_audio"] = generated
    if include_sequence:
        sequence = root / "live-sequence.json"
        write_live_sequence_plan(
            sequence,
            {
                "game_id": "synthetic-game",
                "producer": {"name": "synthetic-extractor", "version": "0.7.0"},
                "source_extract_sha256": "1" * 64,
                "chapters": [
                    {
                        "chapter": "chapter-1",
                        "entry_event_ids": ["event-7"],
                        "events": [
                            {
                                "event_id": "event-7",
                                "sequence": 7,
                                "kind": "speech",
                                "line_id": line_id,
                                "control": "terminal",
                                "successors": [],
                            }
                        ],
                    }
                ],
            },
            story,
        )
        components["live_sequence_plan"] = sequence
    pack_metadata: dict[str, object] = {
        "game": {"id": "synthetic-game", "version": "1.0"},
        "producers": [{"name": "synthetic-extractor", "version": "0.6.0"}],
        "created_at": "2026-08-16T12:05:00Z",
    }
    if semantic_evidence is not None and published_evidence is not None:
        pack_metadata["vntts.authoring"] = {
            "source_audio_semantic_evidence": {
                "path": semantic_evidence.name,
                "sha256": sha256_file(semantic_evidence),
                "evidence_id": published_evidence.evidence_id,
                "entry_count": 1,
            }
        }
    write_game_pack(
        pack_path,
        pack_metadata,
        components,
    )
    return pack_path, line_id, text, text_hash, generated_wav

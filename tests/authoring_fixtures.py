"""Shared authoring input fixtures for tests and the UI catalog."""

import json
from pathlib import Path
from typing import TypedDict

import numpy as np
from vntts_artifacts.audio import (
    PCM16_MONO_WAV_FORMAT,
    probe_pcm16_mono_wav,
    write_pcm16_wav,
)
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import write_generated_audio_manifest
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.story_index import write_story_index_document
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    expected_voice_generation_queue_id,
    write_voice_generation_queue,
)

from vntts.authoring.legacy_import import import_legacy_job
from vntts.authoring.workbench import WorkspaceCreationResult, create_resume_workspace


class LegacyJob(TypedDict):
    schema: str
    schema_version: int
    created_at: str
    updated_at: str
    status: str
    title: str
    targets: list[dict[str, object]]
    story_index: str
    queue: str
    output: str
    voice_manifest: str
    vntts_python: str
    model: str
    narrator_character: str


class LegacyFixture(TypedDict):
    job_directory: Path
    jobs: Path
    job: LegacyJob
    queue: Path
    queue_id: str
    line_id: str
    text_hash: str
    state: Path
    manifest: Path
    wav: Path


def write_legacy_fixture(
    root: Path,
    *,
    job_name: str = "original-job",
    title: str = "Patch 3.7",
    text: str = "Preserve this generated line exactly.",
) -> LegacyFixture:
    root.mkdir(parents=True, exist_ok=True)
    story = root / "story-index.jsonl"
    story.write_text("synthetic story provenance\n", encoding="utf-8")
    voices = root / "voice-manifest.json"
    voices.write_text('{"version": 2, "voices": []}\n', encoding="utf-8")
    text_hash = text_sha256(text)
    line_id = "reverse1999:315401:7"
    queue_id = expected_voice_generation_queue_id(line_id, text_hash)
    queue = root / "shared" / "queue.jsonl"
    write_voice_generation_queue(
        queue,
        {
            "game": "Reverse: 1999",
            "language": "en",
            "generated_at": "2026-08-16T17:00:00+00:00",
        },
        [
            {
                "record_type": "generation_item",
                "queue_id": queue_id,
                "line_id": line_id,
                "text_sha256": text_hash,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "text": text,
                "action": "generate",
                "state": "pending",
                "emotion": "warm",
                "provider_extension": {"keep": True},
            }
        ],
    )
    queue_hash = sha256_file(queue)
    output = root / "shared" / "generated-audio"
    wav = output / "audio" / "rhiannon" / "line.wav"
    write_pcm16_wav(
        wav,
        np.sin(np.linspace(0, 4 * np.pi, 4_000, dtype=np.float32)) * 0.1,
        16_000,
    )
    info = probe_pcm16_mono_wav(wav)
    quality = {
        "duration_seconds": round(info.duration_seconds, 4),
        "sample_rate": info.sample_rate,
        "channels": 1,
        "sample_count": info.sample_count,
        "peak": round(info.peak, 6),
    }
    state_path = output / "generation-state.json"
    state = {
        "schema": "r1999.bulk-generation-state",
        "schema_version": 1,
        "queue_sha256": queue_hash,
        "game": "Reverse: 1999",
        "language": "en",
        "active": {
            "queue_id": queue_id,
            "line_id": line_id,
            "phase": "retrying",
            "attempt": 1,
            "attempt_limit": 3,
            "total_attempts": 4,
            "seed": 12,
            "started_at": "2026-08-16T17:00:00+00:00",
            "updated_at": "2026-08-16T17:00:01+00:00",
            "last_error": "interrupted diagnostic",
        },
        "items": {
            queue_id: {
                "status": "approved",
                "review_status": "approved",
                "attempts": 3,
                "path": "audio/rhiannon/line.wav",
                "line_id": line_id,
                "text_sha256": text_hash,
                "file_sha256": sha256_file(wav),
                "provider": "moss-tts",
                "model": "moss-v1.5",
                "prompt_sha256": "a" * 64,
                "seed": 11,
                "quality": quality,
                "updated_at": "2026-08-16T17:05:00+00:00",
            }
        },
    }
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    manifest = output / "manifest.json"
    write_generated_audio_manifest(
        manifest,
        {
            "game": "Reverse: 1999",
            "language": "en",
            "source_queue_sha256": queue_hash,
            "generated_at": "2026-08-16T17:06:00+00:00",
        },
        [
            {
                "queue_id": queue_id,
                "line_id": line_id,
                "text_sha256": text_hash,
                "audio": "audio/rhiannon/line.wav",
                "audio_format": PCM16_MONO_WAV_FORMAT,
                "audio_sha256": sha256_file(wav),
                "sample_rate": info.sample_rate,
                "sample_count": info.sample_count,
                "provider": "moss-tts",
                "model": "moss-v1.5",
                "prompt_sha256": "a" * 64,
                "seed": 11,
                "review_status": "approved",
            }
        ],
    )
    jobs = root / "jobs"
    job_directory = jobs / job_name
    job_directory.mkdir(parents=True)
    job: LegacyJob = {
        "schema": "r1999.pregeneration-job",
        "schema_version": 1,
        "created_at": "2026-08-16T16:00:00+00:00",
        "updated_at": "2026-08-16T17:00:00+00:00",
        "status": "complete",
        "title": title,
        "targets": [
            {
                "target_id": "hero-story:rhiannon",
                "category": "Character stories",
                "title": "The Eaglet Takes Wing",
                "chapters": ["315401"],
                "episode_count": 1,
                "line_count": 1,
            }
        ],
        "story_index": str(story),
        "queue": str(queue),
        "output": str(output),
        "voice_manifest": str(voices),
        "vntts_python": "/legacy/vntts/python",
        "model": "moss-v1.5",
        "narrator_character": "Matilda",
    }
    (job_directory / "job.json").write_text(
        json.dumps(job, sort_keys=True), encoding="utf-8"
    )
    return {
        "job_directory": job_directory,
        "jobs": jobs,
        "job": job,
        "queue": queue,
        "queue_id": queue_id,
        "line_id": line_id,
        "text_hash": text_hash,
        "state": state_path,
        "manifest": manifest,
        "wav": wav,
    }


def create_test_workspace(
    root: Path, *, text: str | None = None
) -> tuple[LegacyFixture, Path, WorkspaceCreationResult]:
    fixture_options = {} if text is None else {"text": text}
    fixture = write_legacy_fixture(root / "legacy", **fixture_options)
    queue_item = VoiceGenerationQueue.load(fixture["queue"]).items[0]
    side_text = "A source-audio line outside the generation queue."
    write_story_index_document(
        fixture["job"]["story_index"],
        {
            "game": "Reverse: 1999",
            "language": "en",
            "generated_at": "2026-08-16T15:00:00+00:00",
            "collections": [
                {
                    "collection_id": "main",
                    "title": "The Eaglet Takes Wing",
                    "kind": "character-story",
                    "order": 1,
                },
                {
                    "collection_id": "source-only",
                    "title": "Installed source audio",
                    "kind": "reference",
                    "order": 2,
                },
            ],
        },
        [
            {
                "record_type": "line",
                "line_id": queue_item.line_id,
                "text_sha256": queue_item.text_sha256,
                "text": queue_item.text,
                "speaker": queue_item.speaker,
                "voice_character": queue_item.voice_character,
                "kind": "dialogue",
                "chapter": "315401",
                "sequence": 7,
                "collection_id": "main",
                "source_audio_status": "absent",
                "source_audio_reason": "fixture_absent",
                "source_kind": "story",
                "speakable": True,
            },
            {
                "record_type": "line",
                "line_id": "reverse1999:source:1",
                "text_sha256": text_sha256(side_text),
                "text": side_text,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "kind": "dialogue",
                "chapter": "source",
                "sequence": 1,
                "collection_id": "source-only",
                "source_audio_status": "available",
                "source_audio_reason": "fixture_available",
                "source_kind": "story",
                "speakable": True,
            },
        ],
    )
    voice_reference = root / "legacy" / "rhiannon.wav"
    voice_reference.write_bytes(b"voice-reference")
    second_voice_reference = root / "legacy" / "rhiannon-2.wav"
    second_voice_reference.write_bytes(b"second-voice-reference")
    Path(fixture["job"]["voice_manifest"]).write_text(
        json.dumps(
            {
                "version": 2,
                "voices": [
                    {
                        "character": "Rhiannon",
                        "speaker": "Rhiannon",
                        "references": ["rhiannon.wav", "rhiannon-2.wav"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    imported = import_legacy_job(fixture["job_directory"], root / "imports").destination
    workspace = create_resume_workspace(
        imported,
        root / "workspaces",
        story_index=fixture["job"]["story_index"],
        voice_manifest=fixture["job"]["voice_manifest"],
        backend="moss-tts",
        model="model with spaces",
        generation_profile="stable",
        narrator_character="Rhiannon",
    )
    return fixture, imported, workspace

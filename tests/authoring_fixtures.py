"""Shared authoring input fixtures for tests and the UI catalog."""

import json
from copy import deepcopy
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

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.cohort_review import (
    CohortReviewDecision,
    CohortReviewPlan,
    _object,
    _object_list,
    _required_text,
    build_cohort_review_decision,
    build_cohort_review_plan,
    write_cohort_review_plan,
)
from vntts.authoring.legacy_import import import_legacy_job
from vntts.authoring.robustness_corpus import publish_speech_robustness_corpus
from vntts.authoring.workbench import WorkspaceCreationResult, create_resume_workspace
from vntts.authoring.workspace_foundation import load_json_object


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


def create_pending_cohort_workspace(root: Path) -> tuple[Path, Path, str]:
    _fixture, _imported, created = create_test_workspace(root)
    state_path = created.directory / "generated-audio/generation-state.json"
    state = load_json_object(state_path, "fixture generation state")
    items = state["items"]
    assert isinstance(items, dict)
    queue_id, result = next(iter(items.items()))
    assert isinstance(queue_id, str)
    assert isinstance(result, dict)
    result.update(
        {
            "status": "generated",
            "review_status": "pending_review",
            "generation_profile": "stable",
            "voice_character": "Rhiannon",
            "prompt_applied": False,
            "synthesis_provenance_sha256": "b" * 64,
        }
    )
    state["active"] = None
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    return created.directory, state_path, queue_id


def create_failed_reference_workspace(root: Path) -> tuple[Path, str]:
    _fixture, _imported, created = create_test_workspace(root)
    state_path = created.directory / "generated-audio/generation-state.json"
    state = load_json_object(state_path, "fixture generation state")
    items = state["items"]
    assert isinstance(items, dict)
    queue_id, result = next(iter(items.items()))
    assert isinstance(queue_id, str)
    assert isinstance(result, dict)
    for field in ("path", "file_sha256", "quality", "review_status"):
        result.pop(field, None)
    result.update(
        {
            "status": "failed",
            "provider": "moss-tts",
            "model": "model",
            "generation_profile": "stable",
            "voice_character": "Rhiannon",
            "synthesis_provenance_sha256": "a" * 64,
            "failure": {
                "schema_version": 1,
                "kind": "speech_silence",
                "completion": "complete",
                "error_type": "SpeechSilenceValidationError",
                "speech_quality": {
                    "leading_silence_seconds": 0.0,
                    "trailing_silence_seconds": 0.0,
                    "longest_internal_silence_seconds": 2.0,
                    "silence_ratio": 0.4,
                },
                "text_features": {
                    "word_count": 4,
                    "character_count": 20,
                    "sentence_boundary_count": 1,
                    "comma_count": 0,
                    "ellipsis_count": 0,
                },
            },
        }
    )
    state_path.write_text(json.dumps(state, sort_keys=True))
    return created.directory, queue_id


def _legacy_bad_fixture(root: Path) -> tuple[Path, str, Path, Path]:
    workspace, _state, queue_id = create_pending_cohort_workspace(root)
    reviews = workspace / "cohort-reviews"
    reviews.mkdir()
    plan = build_cohort_review_plan(workspace)
    write_cohort_review_plan(plan, reviews / f"plan-{plan.plan_id}.json")
    cohorts = plan.document["cohorts"]
    assert isinstance(cohorts, list)
    cohort = cohorts[0]
    assert isinstance(cohort, dict)
    cohort_id = cohort["cohort_id"]
    assert isinstance(cohort_id, str)
    decision = build_cohort_review_decision(
        plan,
        cohort_id,
        "rejected",
        reviewed_queue_ids=[queue_id],
        sample_assessments={queue_id: "bad"},
    )
    document = deepcopy(decision.document)
    document["schema_version"] = 1
    document.pop("item_review_statuses")
    assessments = document["sample_assessments"]
    assert isinstance(assessments, list)
    assessment = assessments[0]
    assert isinstance(assessment, dict)
    assessment.pop("defect_reasons")
    document["decision_id"] = canonical_document_sha256(
        {key: value for key, value in document.items() if key != "decision_id"}
    )
    decision_path = reviews / f"decision-{document['decision_id']}.json"
    decision_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    corpus = root / "corpus-v3"
    publish_speech_robustness_corpus([reviews], [], corpus)
    return workspace, queue_id, decision_path, corpus


def create_voice_quality_review(
    root: Path,
) -> tuple[Path, Path, str, CohortReviewPlan, CohortReviewDecision]:
    workspace, state_path, queue_id = create_pending_cohort_workspace(root)
    state = load_json_object(state_path, "voice-quality fixture state")
    result = _object(_object(state["items"])[queue_id])
    result.update(
        {
            "provider": "moss-tts",
            "model": "model with spaces",
            "generation_profile": "stable",
        }
    )
    state_path.write_text(json.dumps(state, sort_keys=True))
    return create_voice_quality_review_from_workspace(workspace, state_path, queue_id)


def create_voice_quality_review_from_workspace(
    workspace: Path, state_path: Path, queue_id: str
) -> tuple[Path, Path, str, CohortReviewPlan, CohortReviewDecision]:
    plan = build_cohort_review_plan(workspace)
    cohort = _object_list(plan.document["cohorts"])[0]
    cohort_id = _required_text(cohort["cohort_id"], "Fixture cohort ID")
    decision = build_cohort_review_decision(
        plan,
        cohort_id,
        "accepted",
        reviewed_queue_ids=[queue_id],
        sample_assessments={queue_id: "acceptable"},
    )
    return workspace, state_path, queue_id, plan, decision

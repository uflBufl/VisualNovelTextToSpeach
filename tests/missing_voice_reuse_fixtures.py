"""Shared missing-voice reuse inputs for tests and catalog snapshots."""

from __future__ import annotations

import hashlib
import json
import wave
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import write_generated_audio_manifest
from vntts_artifacts.story_index import write_story_index_document
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    write_voice_generation_queue,
)

from tests.authoring_fixtures import LegacyFixture, write_legacy_fixture
from vntts.authoring.legacy_import import import_legacy_job
from vntts.authoring.missing_voice_policy import MissingVoicePolicy
from vntts.authoring.missing_voice_reuse import (
    MissingVoiceReusePlan,
    build_missing_voice_reuse_plan,
    write_missing_voice_reuse_plan,
)
from vntts.authoring.missing_voice_reuse_binding import (
    publish_missing_voice_reuse_binding,
)
from vntts.authoring.missing_voice_reuse_review import (
    CandidateSnapshot,
    _object,
    _objects,
    build_missing_voice_reuse_review,
)
from vntts.authoring.workbench import create_resume_workspace


def create_missing_voice_reuse_workspace(
    root: Path,
    *,
    text: str | None = None,
    missing_voice_policy: MissingVoicePolicy | Mapping[str, object] | None = None,
) -> tuple[LegacyFixture, Path, Path]:
    fixture = write_legacy_fixture(root / "legacy")
    queue = VoiceGenerationQueue.load(fixture["queue"])
    item = queue.items[0]
    record = item.to_record()
    record["speaker"] = "Aderyn"
    record["voice_character"] = "Aderyn"
    if text is not None:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        record["text"] = text
        record["text_sha256"] = text_hash
        queue_id = f"{item.line_id}:{text_hash[:16]}"
        record["queue_id"] = queue_id
        fixture["queue_id"] = queue_id
    write_voice_generation_queue(fixture["queue"], queue.metadata, [record])
    queue_sha256 = sha256_file(fixture["queue"])

    state: dict[str, object] = json.loads(fixture["state"].read_text(encoding="utf-8"))
    state["queue_sha256"] = queue_sha256
    state["active"] = None
    state["items"] = {}
    fixture["state"].write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    write_generated_audio_manifest(
        fixture["manifest"],
        {
            "game": "Reverse: 1999",
            "language": "en",
            "source_queue_sha256": queue_sha256,
            "generated_at": "2026-08-16T17:06:00+00:00",
        },
        [],
    )
    record_text = record.get("text")
    record_text_hash = record.get("text_sha256")
    if not isinstance(record_text, str) or not isinstance(record_text_hash, str):
        raise TypeError("Missing-voice fixture queue record is malformed")
    write_story_index_document(
        fixture["job"]["story_index"],
        {
            "game": "Reverse: 1999",
            "language": "en",
            "generated_at": "2026-08-16T15:00:00+00:00",
            "collections": [
                {
                    "collection_id": "story",
                    "title": "Aderyn story",
                    "kind": "character-story",
                    "order": 1,
                }
            ],
        },
        [
            {
                "record_type": "line",
                "line_id": item.line_id,
                "text_sha256": record_text_hash,
                "text": record_text,
                "speaker": "Aderyn",
                "voice_character": "Aderyn",
                "kind": "dialogue",
                "chapter": "315401",
                "sequence": 7,
                "collection_id": "story",
                "source_audio_status": "absent",
                "source_audio_reason": "fixture_absent",
                "source_kind": "story",
                "speakable": True,
                "portrait": "314601.png",
            }
        ],
    )
    voice_manifest = Path(fixture["job"]["voice_manifest"])
    (voice_manifest.parent / "adult.wav").write_bytes(b"adult-reference")
    (voice_manifest.parent / "rhiannon.wav").write_bytes(b"rhiannon-reference")
    (voice_manifest.parent / "narrator.wav").write_bytes(b"narrator-reference")
    voice_manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "voices": [
                    {
                        "character": "Adult Aderyn",
                        "speaker": "adult-aderyn",
                        "references": ["adult.wav"],
                    },
                    {
                        "character": "Rhiannon",
                        "speaker": "rhiannon",
                        "references": ["rhiannon.wav"],
                    },
                    {
                        "character": "Centurion",
                        "speaker": "centurion",
                        "references": ["narrator.wav"],
                    },
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
        voice_manifest=voice_manifest,
        backend="moss-tts",
        model="model",
        generation_profile="stable",
        narrator_character="Centurion",
        missing_voice_policy=missing_voice_policy,
    )
    return fixture, imported, workspace.directory


def build_missing_voice_reuse_plan_fixture(
    workspace: str | Path,
) -> MissingVoiceReusePlan:
    return build_missing_voice_reuse_plan(
        workspace,
        "Aderyn",
        cohorts={"adult family": ("314601.png",)},
        candidate_voice_characters=("Adult Aderyn", "Centurion"),
    )


def build_failed_missing_voice_reuse_plan_fixture(
    fixture: LegacyFixture,
    workspace: str | Path,
) -> MissingVoiceReusePlan:
    state_path = Path(workspace) / "generated-audio/generation-state.json"
    state: dict[str, object] = json.loads(state_path.read_text(encoding="utf-8"))
    items = state.get("items")
    if not isinstance(items, dict):
        raise TypeError("Missing-voice fixture generation state is malformed")
    items[fixture["queue_id"]] = {
        "status": "failed",
        "attempts": 3,
        "last_error": "Generated WAV failed speech-silence validation",
    }
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    return build_missing_voice_reuse_plan(
        workspace,
        "Aderyn",
        cohorts={"failed family": ("314601.png",)},
        candidate_voice_characters=("Centurion",),
        failed_queue_ids=(fixture["queue_id"],),
    )


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\x00\x00" * 800)


def create_failed_prompt_hypothesis_review(
    root: Path, *, tamper_prompt: bool = False
) -> tuple[LegacyFixture, Path, Path, Path]:
    fixture, _imported, workspace = create_missing_voice_reuse_workspace(
        root,
        text="What happened? You're hurt.",
        missing_voice_policy={
            "schema_version": 1,
            "mode": "narrator_roles",
            "roles": ["Aderyn"],
        },
    )
    state_path = workspace / "generated-audio/generation-state.json"
    state: dict[str, object] = json.loads(state_path.read_text(encoding="utf-8"))
    items = state.get("items")
    if not isinstance(items, dict):
        raise TypeError("Missing-voice prompt fixture state is malformed")
    items[fixture["queue_id"]] = {
        "status": "failed",
        "attempts": 1,
        "last_error": "Generated WAV failed speech-silence validation",
    }
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    plan = build_missing_voice_reuse_plan(
        workspace,
        "Aderyn",
        cohorts={"failed": ("314601.png",)},
        candidate_voice_characters=("Centurion",),
        failed_queue_ids=(fixture["queue_id"],),
        inline_pause_ms=180,
    )
    plan_path = root / "plan.json"
    write_missing_voice_reuse_plan(plan, plan_path)
    candidate = plan.document["candidates"][0]
    candidate_id = candidate.get("candidate_id")
    voice_character = candidate.get("voice_character")
    if not isinstance(candidate_id, str) or not isinstance(voice_character, str):
        raise TypeError("Missing-voice prompt fixture candidate is malformed")
    render_hypothesis = _object(
        candidate.get("render_hypothesis"),
        "Missing-voice prompt fixture hypothesis",
    )
    prompts = _objects(
        render_hypothesis.get("prompts"), "Missing-voice prompt fixture prompts"
    )
    if not prompts:
        raise TypeError("Missing-voice prompt fixture hypothesis is malformed")
    prompt = prompts[0]
    derived_prompt_sha256 = prompt.get("derived_prompt_sha256")
    marker_count = prompt.get("marker_count")
    if not isinstance(derived_prompt_sha256, str) or not isinstance(marker_count, int):
        raise TypeError("Missing-voice prompt fixture prompt is malformed")
    candidate_root = (root / "candidate").resolve()
    audio = candidate_root / "generated-audio/audio/sample.wav"
    _write_wav(audio)
    derived = "f" * 64 if tamper_prompt else derived_prompt_sha256
    item: dict[str, object] = {
        "status": "generated",
        "attempts": 2,
        "path": "audio/sample.wav",
        "file_sha256": sha256_file(audio),
        "quality": {"duration_seconds": 0.1},
        "source_reference_binding": {
            "queue_id": fixture["queue_id"],
            "synthesis_voice_character": voice_character,
        },
        "failure_repair": {
            "strategy": "inline_pause_marker",
            "pause_ms": 180,
            "marker_count": marker_count,
            "derived_prompt_sha256": derived,
        },
        "synthesis_text_sha256": derived,
    }
    snapshot: CandidateSnapshot = {
        "directory": candidate_root,
        "workspace": {"workspace_id": "candidate-workspace"},
        "state": {"items": {fixture["queue_id"]: item}},
        "authority": {
            "path": str(candidate_root),
            "workspace_id": "candidate-workspace",
            "workspace_sha256": "1" * 64,
            "state_sha256": "2" * 64,
            "voice_manifest_sha256": "3" * 64,
        },
    }
    with patch(
        "vntts.authoring.missing_voice_reuse_review._load_candidate_workspace",
        return_value=snapshot,
    ):
        session = build_missing_voice_reuse_review(
            plan_path,
            {candidate_id: (candidate_root,)},
            root / "review",
        )
    return fixture, workspace, plan_path, session


def create_missing_voice_reuse_review_fixture(
    root: Path,
    statuses: Sequence[str] = ("generated", "failed"),
) -> tuple[
    Path,
    dict[str, tuple[Path, ...]],
    dict[Path, CandidateSnapshot],
    str,
]:
    fixture, _imported, workspace = create_missing_voice_reuse_workspace(root)
    plan = build_missing_voice_reuse_plan_fixture(workspace)
    plan_path = root / "plan.json"
    write_missing_voice_reuse_plan(plan, plan_path)
    queue_id = fixture["queue_id"]
    snapshots: dict[Path, CandidateSnapshot] = {}
    evidence: dict[str, tuple[Path, ...]] = {}
    for index, (candidate, status) in enumerate(
        zip(plan.document["candidates"], statuses, strict=True), start=1
    ):
        candidate_root = root / f"candidate-{index}"
        candidate_root.mkdir()
        candidate_id = candidate["candidate_id"]
        assert isinstance(candidate_id, str)
        evidence[candidate_id] = (candidate_root,)
        item: dict[str, object] = {
            "status": status,
            "attempts": 1,
            "provider": "moss-tts",
            "model": "/models/moss-test",
            "generation_profile": "stable",
            "seed": 0,
            "source_reference_binding": {
                "queue_id": queue_id,
                "synthesis_voice_character": candidate["voice_character"],
            },
        }
        if status == "generated":
            audio = candidate_root / "generated-audio/audio/sample.wav"
            _write_wav(audio)
            item.update(
                {
                    "path": "audio/sample.wav",
                    "file_sha256": sha256_file(audio),
                    "quality": {"duration_seconds": 0.1},
                }
            )
        else:
            item.update(
                {
                    "failure": {"kind": "missed_eos_audio_limit"},
                    "last_error": "Typed limited render",
                }
            )
        snapshots[candidate_root.resolve()] = {
            "directory": candidate_root.resolve(),
            "workspace": {
                "workspace_id": f"workspace-{index}",
                "run_config": {
                    "backend": "moss-tts",
                    "model": "/models/moss-test",
                    "generation_profile": "stable",
                },
            },
            "state": {"items": {queue_id: item}},
            "authority": {
                "path": str(candidate_root.resolve()),
                "workspace_id": f"workspace-{index}",
                "workspace_sha256": f"{index}" * 64,
                "state_sha256": f"{index + 2}" * 64,
                "voice_manifest_sha256": f"{index + 4}" * 64,
            },
        }
    return plan_path, evidence, snapshots, queue_id


def create_missing_voice_reuse_binding_review(
    root: Path, statuses: Sequence[str] = ("generated", "failed")
) -> tuple[Path, Path, str]:
    plan_path, evidence, snapshots, queue_id = (
        create_missing_voice_reuse_review_fixture(root, statuses=statuses)
    )
    with patch(
        "vntts.authoring.missing_voice_reuse_review._load_candidate_workspace",
        side_effect=lambda _plan, _candidate, path: snapshots[Path(path).resolve()],
    ):
        session_path = build_missing_voice_reuse_review(
            plan_path, evidence, root / "review", seed=7
        )
    return plan_path, session_path, queue_id


def create_missing_voice_live_fallback_fixture(root: Path) -> tuple[Path, Path, str]:
    plan_path, session_path, queue_id = create_missing_voice_reuse_binding_review(
        root, statuses=("failed", "failed")
    )
    binding = publish_missing_voice_reuse_binding(
        plan_path, session_path, root / "binding"
    ).directory
    plan: dict[str, object] = json.loads(plan_path.read_text(encoding="utf-8"))
    source = plan.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("workspace"), str):
        raise TypeError("Missing-voice fixture plan source is malformed")
    workspace = Path(source["workspace"])
    return workspace, binding, queue_id

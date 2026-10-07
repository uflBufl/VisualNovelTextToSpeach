"""Shared parallel-workspace and terminal-conflict review inputs."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav

from tests.authoring_fixtures import create_test_workspace
from vntts.authoring.bulk_generation import inspect_generated_wav
from vntts.authoring.cohort_bundle import (
    build_cohort_review_bundle,
    execute_cohort_bundle_decision,
    validate_cohort_review_bundle_document,
    write_cohort_review_bundle,
)
from vntts.authoring.reconciliation import (
    build_authoring_reconciliation,
    write_authoring_reconciliation,
)
from vntts.authoring.terminal_conflict_review import publish_terminal_conflict_review
from vntts.authoring.workbench import create_resume_workspace
from vntts.authoring.workspace_foundation import load_json_object


def create_parallel_review_workspaces(root: Path) -> tuple[Path, Path, str, Path, Path]:
    _fixture, imported, primary = create_test_workspace(root)
    primary_directory = primary.directory
    secondary = create_resume_workspace(
        imported,
        root / "workspaces",
        story_index=primary_directory / "inputs/story-index.jsonl",
        voice_manifest=primary_directory / "inputs/voice/manifest.json",
        narrator_character="Rhiannon",
        backend="moss-tts",
        model="model with spaces",
        generation_profile="alternate",
    ).directory
    queue_id = None
    for workspace, profile in (
        (primary_directory, "stable"),
        (secondary, "alternate"),
    ):
        state_path = workspace / "generated-audio/generation-state.json"
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
                "generation_profile": profile,
                "voice_character": "Rhiannon",
                "prompt_applied": False,
                "synthesis_provenance_sha256": "b" * 64,
            }
        )
        state["active"] = None
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    bundles = root / "review-bundles"
    bundles.mkdir()
    publication = bundles / "parallel.json"
    write_cohort_review_bundle(
        build_cohort_review_bundle((primary_directory, secondary)), publication
    )
    assert queue_id is not None
    return primary_directory, secondary, queue_id, bundles, publication


def decide_parallel_review_bundle(
    publication: Path, decisions: Sequence[tuple[str, str]]
) -> None:
    bundle = validate_cohort_review_bundle_document(
        json.loads(publication.read_text(encoding="utf-8"))
    )
    for workspace_id, decision in decisions:
        cohort = next(
            value
            for value in bundle.document["cohorts"]
            if value["workspace_id"] == workspace_id
        )
        cohort_id = cohort["cohort_id"]
        assert isinstance(cohort_id, str)
        samples = cohort["samples"]
        assert isinstance(samples, list)
        sample = samples[0]
        assert isinstance(sample, dict)
        queue_id = sample["queue_id"]
        assert isinstance(queue_id, str)
        projection = execute_cohort_bundle_decision(
            bundle,
            workspace_id,
            cohort_id,
            decision,
            reviewed_queue_ids=[queue_id],
        )
        bundle = projection.next_bundle


def create_terminal_conflict_fixture(root: Path) -> tuple[Path, Path, str, Path]:
    primary, secondary, queue_id, bundles, publication = (
        create_parallel_review_workspaces(root)
    )
    publication.unlink()
    state_path = secondary / "generated-audio/generation-state.json"
    state = load_json_object(state_path, "fixture generation state")
    items = state["items"]
    assert isinstance(items, dict)
    result = items[queue_id]
    assert isinstance(result, dict)
    audio_path = result["path"]
    assert isinstance(audio_path, str)
    audio = secondary / "generated-audio" / audio_path
    samples = np.linspace(-0.25, 0.25, 4_000, dtype=np.float32)
    write_pcm16_wav(audio, samples, 16_000)
    result["file_sha256"] = hashlib.sha256(audio.read_bytes()).hexdigest()
    result["quality"] = asdict(inspect_generated_wav(audio))
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    write_cohort_review_bundle(
        build_cohort_review_bundle((primary, secondary)), publication
    )
    decide_parallel_review_bundle(
        publication,
        ((primary.name, "accepted"), (secondary.name, "rejected")),
    )
    report = build_authoring_reconciliation(primary, bundles)
    report_path = root / "reconciliation.json"
    write_authoring_reconciliation(report, report_path)
    return primary, secondary, queue_id, report_path


def create_terminal_conflict_review(root: Path) -> Path:
    _primary, _secondary, _queue_id, report = create_terminal_conflict_fixture(root)
    directory = root / "conflict-review"
    publish_terminal_conflict_review(report, directory)
    return directory

"""Shared pregeneration pack inputs and reviewed synthetic output for tests."""

from __future__ import annotations

import hashlib
import io
import wave
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import write_story_index_document
from vntts_artifacts.voice_manifest import write_voice_manifest

from tests.bulk_generation_fixtures import SyntheticRenderer, write_queue
from tests.story_fixtures import write_content
from tests.voice_manifest_fixtures import write_manifest
from vntts.authoring.audio_events import audio_event_plan_for_record
from vntts.authoring.bulk_generation import (
    authorize_live_fallback,
    is_spoken_queue_item,
    review_generation_item,
    run_bulk_generation,
)
from vntts.pregeneration_generation import OfflineGenerationResult
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_setup import (
    PregenerationJob,
    PregenerationJobStore,
    PreparationEstimate,
    inspect_story_index,
)
from vntts.pregeneration_voices import VoiceGroup, VoicePlan, VoicePlanStore
from vntts.settings import AppSettings
from vntts.synthesis import (
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisRequest,
    SynthesisResult,
    SynthesisTiming,
)
from vntts.voices import CharacterVoiceRegistry


def item(name: str, sequence: int) -> dict[str, object]:
    text = f"Prepared line {name}."
    text_sha256 = hashlib.sha256(text.encode()).hexdigest()
    return {
        "record_type": "generation_item",
        "queue_id": f"pack:{name}:{text_sha256[:16]}",
        "line_id": f"pack:{name}",
        "text_sha256": text_sha256,
        "text": text,
        "speaker": "Narrator",
        "voice_character": "Narrator",
        "action": "generate",
        "prompt_adapters": {},
        "sequence": sequence,
    }


def fixture(
    root: Path,
    names: Iterable[str] = ("generated", "fallback"),
    *,
    include_omission: bool = False,
    omission_source_audio_status: str = "absent",
) -> tuple[
    PregenerationJob,
    PregenerationInput,
    OfflineGenerationResult,
    tuple[dict[str, object], ...],
]:
    identity = "a" * 64
    directory = root / f"generation-input-{identity[:16]}"
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir()
    values = [item(name, sequence) for sequence, name in enumerate(names, 1)]
    if include_omission:
        text = "*chirp*"
        event = item("omission", len(values) + 1)
        text_sha256 = hashlib.sha256(text.encode()).hexdigest()
        event.update(
            text=text,
            text_sha256=text_sha256,
            source_audio_status=omission_source_audio_status,
            source_audio_reason=f"fixture_{omission_source_audio_status}",
        )
        event["action"] = {
            "absent": "generate",
            "unavailable": "prefer_source_audio",
            "unknown": "resolve_audio",
        }[omission_source_audio_status]
        event["queue_id"] = f"pack:omission:{text_sha256[:16]}"
        event["vntts.authoring.audio_event_plan"] = audio_event_plan_for_record(event)
        values.append(event)
    items = tuple(values)
    story = directory / "story-index.jsonl"
    write_story_index_document(
        story,
        {"game": "Synthetic Game", "language": "en"},
        [
            {
                "record_type": "line",
                "line_id": value["line_id"],
                "chapter": "1",
                "sequence": value["sequence"],
                "speaker": value["speaker"],
                "voice_character": value["voice_character"],
                "text": value["text"],
                "kind": "dialogue",
                "source_audio_status": value.get("source_audio_status", "absent"),
                "source_audio_reason": value.get(
                    "source_audio_reason", "fixture_absent"
                ),
                "speakable": True,
            }
            for value in items
        ],
    )
    voices = directory / "voice-manifest.json"
    write_voice_manifest(
        voices,
        {
            "version": 2,
            "voices": [
                {
                    "character": "Narrator",
                    "speaker": "alba",
                    "aliases": [],
                    "references": [],
                }
            ],
        },
    )
    queue = write_queue(directory / "queue.jsonl", items)
    omission_queue_ids: tuple[str, ...] = ()
    if include_omission:
        omission_queue_id = items[-1]["queue_id"]
        assert isinstance(omission_queue_id, str)
        omission_queue_ids = (omission_queue_id,)
    generation_input = PregenerationInput(
        identity,
        directory,
        story,
        voices,
        queue,
        sha256_file(queue),
        2,
        2,
        (),
        sha256_file(story),
        sha256_file(voices),
        None,
        audio_event_omission_queue_ids=omission_queue_ids,
    )
    output = root / f"generation-output-{identity[:16]}"
    renderer = SyntheticRenderer(
        [SynthesisCompletion.COMPLETE, SynthesisCompletion.LIMITED]
    )
    renderer.name = "pocket-tts"
    renderer.model_name = "pocket-tts"
    generated = run_bulk_generation(
        queue,
        output,
        renderer,
        provider="pocket-tts",
        model="pocket-tts",
        generation_profile="default",
        retries=0,
        item_filter=is_spoken_queue_item,
    )
    approved_queue_id = items[0]["queue_id"]
    assert isinstance(approved_queue_id, str)
    review_generation_item(generated.state, approved_queue_id, "approved")
    fallback_queue_id = items[1]["queue_id"]
    assert isinstance(fallback_queue_id, str)
    authorize_live_fallback(
        generated.state,
        queue,
        fallback_queue_id,
        reason="automatic_recovery_exhausted",
        model="pocket-tts",
    )
    result = OfflineGenerationResult(
        output,
        generated.state,
        generated.manifest,
        1,
        0,
        1,
    )
    selected_line_ids: list[str] = []
    for value in items:
        line_id = value["line_id"]
        assert isinstance(line_id, str)
        selected_line_ids.append(line_id)
    job = PregenerationJob(
        job_id="b" * 24,
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
        status="planned",
        provider_id="synthetic",
        game="Synthetic Game",
        game_version="1.0",
        story_index=str(story),
        story_index_sha256=sha256_file(story),
        selected_story_ids=("chapter-1",),
        selected_line_ids=tuple(selected_line_ids),
        estimate=PreparationEstimate(2, 0, 2, 1, 1, 1000),
    )
    return job, generation_input, result, items


class CollectedResult:
    def __init__(self, result: SynthesisResult) -> None:
        self.result = result

    def collect(self) -> SynthesisResult:
        return self.result


class FakeBackend:
    def __init__(
        self,
        name: str,
        *,
        completion: SynthesisCompletion = SynthesisCompletion.COMPLETE,
        on_render: Callable[[], object] | None = None,
        pcm: NDArray[np.float32] | None = None,
        result_sample_rate: int = 16_000,
    ) -> None:
        self.name = name
        self.completion = completion
        self.on_render = on_render
        self.pcm = pcm
        self.result_sample_rate = result_sample_rate
        self.registry: CharacterVoiceRegistry | None = None
        self.requests: list[SynthesisRequest] = []
        self.shutdown_count = 0

    def render(self, request: SynthesisRequest) -> CollectedResult:
        self.requests.append(request)
        if self.on_render is not None:
            self.on_render()
        return CollectedResult(
            SynthesisResult(
                pcm=(
                    np.full(1_600, 0.1, dtype=np.float32)
                    if self.pcm is None
                    else self.pcm
                ),
                sample_rate=self.result_sample_rate,
                completion=self.completion,
                limits=SynthesisLimits(None, None),
                timing=SynthesisTiming(10.0, 100.0),
                diagnostics=SynthesisDiagnostics(
                    backend=self.name,
                    cache_source="generated",
                    generation_profile=request.generation_profile,
                    seed=request.seed,
                    chunk_count=1,
                    sample_count=1_600,
                ),
            )
        )

    def shutdown(self) -> None:
        self.shutdown_count += 1


def clean_wav_bytes(
    *, amplitude: float = 0.1, seconds: float = 1.2, sample_rate: int = 16_000
) -> bytes:
    samples = np.full(round(seconds * sample_rate), amplitude, dtype=np.float32)
    samples[1::2] *= -1
    pcm = np.round(samples * 32767).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(pcm.tobytes())
    return output.getvalue()


def ambiguous_fixture(root: Path) -> tuple[VoicePlan, VoiceGroup, Path]:
    content = inspect_story_index(write_content(root / "content"))
    jobs = PregenerationJobStore(root / "jobs")
    job = jobs.create_or_resume(content, ("story",))
    manifest = write_manifest(root / "voices", rhiannon=clean_wav_bytes())
    plan = VoicePlanStore(jobs).create(
        job,
        AppSettings(speech_backend="moss-tts", tts_profile="stable"),
        manifest_path=manifest,
    )
    selected = next(group for group in plan.groups if group.character == "Rhiannon")
    ambiguous = replace(
        selected,
        route="needs-audition",
        resolution="ambiguous-voice-evidence",
    )
    plan = replace(
        plan,
        groups=tuple(
            ambiguous if group is selected else group for group in plan.groups
        ),
    )
    return plan, ambiguous, manifest

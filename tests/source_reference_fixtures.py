"""Shared source-reference review inputs for tests and catalog snapshots."""

import hashlib
import json
import struct
import threading
import zlib
from collections.abc import Callable, Generator
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import probe_pcm16_mono_wav, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.story_index import write_story_index_document

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import BulkGenerationResult, run_bulk_generation
from vntts.authoring.source_reference_quality import (
    QUALITY_REVIEW_SCHEMA,
    QUALITY_REVIEW_VERSION,
    SourceReferenceQualityResult,
    publish_source_reference_quality_review,
)
from vntts.authoring.source_reference_review import (
    SourceReferenceEvaluationResult,
    SourceReferencePlanResult,
    import_source_reference_review,
    publish_source_reference_evaluation,
)
from vntts.synthesis import (
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisRequest,
    SynthesisResult,
    SynthesisTiming,
)
from vntts.voices import CharacterVoiceRegistry


def _write_audio(root: Path, name: str, value: float) -> dict[str, object]:
    path = root / name
    write_pcm16_wav(path, np.full(800, value, dtype=np.float32), 16_000)
    info = probe_pcm16_mono_wav(path)
    return {
        "audio": name,
        "audio_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "sample_rate": info.sample_rate,
        "sample_count": info.sample_count,
        "duration_seconds": round(info.duration_seconds, 6),
    }


def _write_png(root: Path, name: str) -> dict[str, object]:
    path = root / name
    write_test_png(path, red=127, green=48, blue=16)
    return {
        "image": name,
        "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "width": 2,
        "height": 2,
    }


def write_quality_session(root: Path) -> Path:
    reference = _write_audio(root, "reference.wav", 0.1)
    generated_one = _write_audio(root, "generated-one.wav", 0.2)
    generated_two = _write_audio(root, "generated-two.wav", 0.3)
    portrait = _write_png(root, "portrait.png")
    now = datetime.now(timezone.utc).isoformat()
    samples = []
    for index, audio in enumerate((generated_one, generated_two), start=1):
        text = f"Generated sample {index}."
        samples.append(
            {
                "queue_id": f"queue-{index}",
                "evaluation_kind": ("source-match" if index == 1 else "fixed-1"),
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                **audio,
            }
        )
    session = root / "review.json"
    session.write_text(
        json.dumps(
            {
                "schema": QUALITY_REVIEW_SCHEMA,
                "schema_version": QUALITY_REVIEW_VERSION,
                "created_at": now,
                "updated_at": now,
                "source_reference_plan_sha256": "1" * 64,
                "source_reference_evaluation_sha256": "2" * 64,
                "generation_state_sha256": "3" * 64,
                "variant_count": 1,
                "completed_count": 0,
                "variants": [
                    {
                        "variant_id": "cluster-a-anchor-1",
                        "cluster_id": "cluster-a",
                        "character": "Dobharchu",
                        "portrait": "534704",
                        "portrait_image": portrait,
                        "source_bank": "hero.bnk",
                        "media_id": 123,
                        "affected_queue_item_count": 37,
                        "reference": reference,
                        "generated_samples": samples,
                        "excluded_results": [],
                        "decision": None,
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return session


class EvaluationRenderer:
    name = "synthetic"
    model_name = "synthetic-v1"

    def __init__(self) -> None:
        self.requests: list[SynthesisRequest] = []

    def render(self, request: SynthesisRequest) -> SynthesisChunkStream:
        self.requests.append(request)
        pcm = np.sin(
            np.linspace(0, 20, 4_000, dtype=np.float32), dtype=np.float32
        ) * np.float32(0.2)

        def produce() -> Generator[SynthesisChunk, None, SynthesisResult]:
            yield SynthesisChunk(pcm, 16_000, 0, 1.0)
            return SynthesisResult(
                pcm=pcm,
                sample_rate=16_000,
                completion=SynthesisCompletion.COMPLETE,
                limits=SynthesisLimits(256, 180.0),
                timing=SynthesisTiming(1.0, 2.0),
                diagnostics=SynthesisDiagnostics(
                    backend=self.name,
                    cache_source="fresh-generation",
                    generation_profile=request.generation_profile,
                    seed=request.seed,
                    chunk_count=1,
                    sample_count=len(pcm),
                ),
            )

        return SynthesisChunkStream(produce())

    def stop(self) -> None:
        pass


def write_test_png(path: Path, *, red: int, green: int = 40, blue: int = 20) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    payload = b"\x89PNG\r\n\x1a\n"
    payload += chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
    row = b"\x00" + bytes((red, green, blue, 255)) * 2
    payload += chunk(b"IDAT", zlib.compress(row * 2))
    payload += chunk(b"IEND", b"")
    path.write_bytes(payload)


def candidate_key(
    character: str, portrait: str, bank: str, media_id: int, reference_sha256: str
) -> str:
    identity = json.dumps(
        [character, portrait, bank, media_id, reference_sha256],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def write_source_reference_review_inputs(
    root: str | Path,
    *,
    shared_portrait_bank: bool = False,
    character: str = "Hero",
    line_prefix: str = "",
) -> tuple[Path, Path, Path]:
    root = Path(root)
    references = root / "references"
    references.mkdir()
    candidates: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    accepted_young_bank = "hero-adult.bnk" if shared_portrait_bank else "hero-young.bnk"
    for index, (portrait, bank, decision) in enumerate(
        (
            ("adult.png", "hero-adult.bnk", "accept"),
            ("young.png", accepted_young_bank, "accept"),
            ("adult.png", "hero-adult.bnk", "reject"),
        ),
        start=1,
    ):
        reference = references / f"{index}.wav"
        values = np.sin(np.linspace(0, 20 + index, 4_000, dtype=np.float32)) * 0.2
        write_pcm16_wav(reference, values, 16_000)
        reference_sha256 = hashlib.sha256(reference.read_bytes()).hexdigest()
        candidate = {
            "character": character,
            "portrait": portrait,
            "source_bank": bank,
            "media_id": index,
            "reference": f"references/{index}.wav",
            "reference_sha256": reference_sha256,
            "technical_pass": True,
            "transcript_conflict": False,
            "source_lines": [
                {
                    "line_id": f"source:{index}",
                    "text": f"Source transcript {index}",
                }
            ],
        }
        evidence_sha256 = hashlib.sha256(
            json.dumps(
                candidate,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        key = candidate_key(character, portrait, bank, index, reference_sha256)
        candidates.append(candidate)
        decisions.append(
            {
                "candidate_key": key,
                "candidate_evidence_sha256": evidence_sha256,
                "reference_sha256": reference_sha256,
                "decision": decision,
                "notes": "exact human decision",
            }
        )
    report = root / "report.json"
    report.write_text(
        json.dumps(
            {
                "schema": "r1999.story-voice-reference-candidates",
                "schema_version": 1,
                "groups": [],
                "candidates": candidates,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    review = root / "review.json"
    review.write_text(
        json.dumps(
            {
                "schema": "r1999.story-voice-reference-review",
                "schema_version": 2,
                "candidate_report_sha256": hashlib.sha256(
                    report.read_bytes()
                ).hexdigest(),
                "decisions": decisions,
                "invalidated_decisions": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    records: list[dict[str, object]] = []
    for index, portrait in enumerate(("adult.png", "young.png", "other.png"), start=1):
        text = f"Missing target {index}."
        records.append(
            {
                "record_type": "line",
                "line_id": f"{line_prefix}target:{index}",
                "chapter": "one",
                "sequence": index,
                "speaker": character,
                "voice_character": character,
                "text": text,
                "text_sha256": text_sha256(text),
                "kind": "dialogue",
                "source_audio_status": "absent",
                "source_audio_reason": "fixture",
                "source_kind": "story",
                "speakable": True,
                "collection_id": "main",
                "portrait": portrait,
            }
        )
    story = root / "story.jsonl"
    write_story_index_document(
        story,
        {
            "game": "Synthetic",
            "language": "en",
            "generated_at": "2026-08-18T00:00:00+00:00",
            "collections": [
                {
                    "collection_id": "main",
                    "title": "Main",
                    "kind": "story",
                    "order": 1,
                }
            ],
        },
        records,
    )
    return report, review, story


def publish_source_reference_quality_fixture(
    root: Path,
    *,
    portrait_directory: str | Path | None = None,
    shared_portrait_bank: bool = False,
    character: str = "Hero",
    line_prefix: str = "",
) -> tuple[
    SourceReferencePlanResult,
    SourceReferenceEvaluationResult,
    BulkGenerationResult,
    SourceReferenceQualityResult,
]:
    report, review, story = write_source_reference_review_inputs(
        root,
        shared_portrait_bank=shared_portrait_bank,
        character=character,
        line_prefix=line_prefix,
    )
    plan = import_source_reference_review(report, review, story, root / "plan")
    evaluation = publish_source_reference_evaluation(
        plan.directory, root / "evaluation"
    )
    generation = run_bulk_generation(
        evaluation.directory / "queue.jsonl",
        root / "generation",
        EvaluationRenderer(),
        provider="synthetic",
        model="synthetic-v1",
        generation_profile="stable",
    )
    quality = publish_source_reference_quality_review(
        plan.directory,
        evaluation.directory,
        generation.state,
        root / "quality",
        portrait_directory=portrait_directory,
    )
    return plan, evaluation, generation, quality


class DeferredCollectedResult:
    def __init__(self, result_factory: Callable[[], SynthesisResult]) -> None:
        self.result_factory = result_factory

    def collect(self) -> SynthesisResult:
        return self.result_factory()


class PreviewBackend:
    def __init__(
        self,
        name: str,
        registry: CharacterVoiceRegistry,
        model_name: str | None,
        cancellation: threading.Event | None,
        *,
        on_render: Callable[["PreviewBackend", SynthesisRequest], None] | None = None,
    ) -> None:
        self.name = name
        self.registry = registry
        self.model_name = model_name
        self.cancellation = cancellation
        self.on_render = on_render
        self.requests: list[SynthesisRequest] = []
        self.stop_calls = 0

    def render(self, request: SynthesisRequest) -> DeferredCollectedResult:
        self.requests.append(request)

        def result() -> SynthesisResult:
            if self.on_render is not None:
                self.on_render(self, request)
            completion = (
                SynthesisCompletion.CANCELLED
                if request.cancellation_requested()
                else SynthesisCompletion.COMPLETE
            )
            return SynthesisResult(
                pcm=np.full((800, 1), 0.2, dtype=np.float32),
                sample_rate=16_000,
                completion=completion,
                limits=SynthesisLimits(100, 2.0),
                timing=SynthesisTiming(10.0, 20.0),
                diagnostics=SynthesisDiagnostics(
                    backend=self.name,
                    cache_source="fresh-generation",
                    generation_profile=request.generation_profile,
                    seed=request.seed,
                    chunk_count=1,
                    sample_count=800,
                ),
            )

        return DeferredCollectedResult(result)

    def stop(self) -> None:
        self.stop_calls += 1


class PreviewBackendFactory:
    def __init__(
        self,
        *,
        on_render: Callable[[PreviewBackend, SynthesisRequest], None] | None = None,
    ) -> None:
        self.on_render = on_render
        self.backends: list[PreviewBackend] = []

    def __call__(
        self,
        name: str,
        registry: CharacterVoiceRegistry,
        _cache_root: Path,
        *,
        model_name: str | None = None,
        startup_cancellation: threading.Event | None = None,
        **_options: object,
    ) -> PreviewBackend:
        backend = PreviewBackend(
            name,
            registry,
            model_name,
            startup_cancellation,
            on_render=self.on_render,
        )
        self.backends.append(backend)
        return backend


def write_reference_render_comparison_fixture(
    root: Path, *, reference_format: str = "wav"
) -> str:
    root.mkdir()
    controls = root / "controls"
    controls.mkdir()
    reference = controls / f"reference.{reference_format}"
    if reference_format == "wav":
        write_pcm16_wav(reference, np.full(1_200, 0.1, dtype=np.float32), 24_000)
    else:
        reference.write_bytes(b"OggS\x00checksum-bound-fixture")
    reference_sha = sha256_file(reference)
    text_sha = hashlib.sha256(b"A measured test line.").hexdigest()
    queue_id = "reverse1999:1:2:" + text_sha[:16]
    reports: list[str] = []
    arms: list[dict[str, object]] = []
    for index, arm_id in enumerate(("reference-02", "reference-03"), start=1):
        arm_root = root / "arms" / arm_id
        (arm_root / "audio").mkdir(parents=True)
        base: dict[str, object] = {
            "id": queue_id,
            "line_id": "reverse1999:1:2",
            "text": "A measured test line.",
            "text_sha256": text_sha,
            "case_group_id": "b" * 64,
            "candidate_group_id": "c" * 64,
            "candidate_id": "candidate-one",
            "reference_sha256": reference_sha,
        }
        if index == 1:
            audio = arm_root / "audio/0001.wav"
            write_pcm16_wav(audio, np.full(2_400, 0.2, dtype=np.float32), 24_000)
            render = {
                **base,
                "outcome": "complete",
                "audio": "audio/0001.wav",
                "audio_sha256": sha256_file(audio),
                "sample_rate": 24_000,
                "backend": "moss-tts",
                "model": "fixture",
                "generation_profile": "stable",
                "seed": 0,
            }
        else:
            render = {**base, "outcome": "error", "error": "typed limited"}
        report = {
            "schema": "vntts.voice-model-report",
            "schema_version": 1,
            "model_id": arm_id,
            "provider": "reference-render-comparison",
            "backend": "reference-render-comparison",
            "model": "one exact alternative reference per sample",
            "samples": [render],
        }
        report_path = arm_root / "report.json"
        atomic_write_json(report_path, report)
        report_relative = f"arms/{arm_id}/report.json"
        reports.append(report_relative)
        arms.append(
            {
                "arm_id": arm_id,
                "report": report_relative,
                "report_sha256": sha256_file(report_path),
                "complete_count": int(index == 1),
                "failure_count": int(index != 1),
                "renders": [render],
            }
        )
    body: dict[str, object] = {
        "schema": "vntts.authoring-reference-render-comparison",
        "schema_version": 1,
        "generated_at": "2026-08-27T00:00:00+00:00",
        "input_plan": "/immutable/plan.json",
        "input_plan_sha256": "d" * 64,
        "audit": "/immutable/audit",
        "audit_id": "e" * 64,
        "audit_sha256": "f" * 64,
        "queue_ids": [queue_id],
        "controls": [
            {
                "group_id": "c" * 64,
                "candidate_id": "candidate-one",
                "audio": f"controls/reference.{reference_format}",
                "sha256": reference_sha,
            }
        ],
        "arms": arms,
        "reports": reports,
        "complete_pair_queue_ids": [],
    }
    atomic_write_json(
        root / "comparison.json",
        {**body, "comparison_id": canonical_document_sha256(body)},
    )
    return queue_id

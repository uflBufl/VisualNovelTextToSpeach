"""Shared voice manifests and exact reference audio for pregeneration tests."""

import json
import wave
from collections.abc import Sequence
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import (
    expected_voice_generation_queue_id,
    text_sha256,
)

from vntts.authoring.source_reference_bindings import (
    SOURCE_REFERENCE_BINDINGS_FIELD,
    SOURCE_REFERENCE_BINDINGS_SCHEMA,
    SOURCE_REFERENCE_BINDINGS_VERSION,
    queue_voice_overrides_sha256,
)
from vntts.pregeneration_voices import PLAYER_VOICE_CANDIDATES_FIELD


def write_reference(path: Path, payload: bytes) -> None:
    if payload.startswith(b"RIFF"):
        path.write_bytes(payload)
        return
    frames = (payload * ((32_000 // len(payload)) + 1))[:32_000]
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        audio.writeframes(frames)


def write_manifest(
    root: Path, *, rhiannon: bytes = b"rhiannon", unrelated: bytes = b"unrelated"
) -> Path:
    references = root / "references"
    references.mkdir(parents=True, exist_ok=True)
    write_reference(references / "rhiannon.wav", rhiannon)
    write_reference(references / "centurion.wav", b"centurion")
    write_reference(references / "unrelated.wav", unrelated)
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "voices": [
                    {
                        "character": "Rhiannon",
                        "speaker": "rhiannon-v1",
                        "aliases": ["Aderyn"],
                        "references": ["references/rhiannon.wav"],
                    },
                    {
                        "character": "Centurion",
                        "speaker": "centurion-v1",
                        "aliases": [],
                        "references": ["references/centurion.wav"],
                    },
                    {
                        "character": "Unrelated",
                        "speaker": "unrelated-v1",
                        "aliases": [],
                        "references": ["references/unrelated.wav"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def write_conflicting_manifest(
    root: Path, *, bind_selected_lines: bool = False
) -> Path:
    references = root / "references"
    references.mkdir(parents=True, exist_ok=True)
    payloads = {
        "rhiannon.wav": b"rhiannon",
        "adult.wav": b"adult",
        "child.wav": b"child",
    }
    for name, payload in payloads.items():
        write_reference(references / name, payload)
    adult_voice = "Source reference Rhiannon adult"
    child_voice = "Source reference Rhiannon child"
    adult_queue_ids = ["historical:adult"]
    if bind_selected_lines:
        adult_queue_ids = [
            expected_voice_generation_queue_id(
                line_id,
                text_sha256(text),
            )
            for line_id, text in (
                (
                    "line:rhiannon:1",
                    "This is the most useful preview sentence for my voice.",
                ),
                ("line:rhiannon:2", "Short."),
            )
        ]
    overrides = {
        **{queue_id: adult_voice for queue_id in adult_queue_ids},
        "historical:child": child_voice,
    }
    variants = [
        {
            "variant_id": "a" * 64,
            "cluster_id": "adult-cluster",
            "character": "Rhiannon",
            "portrait": "10",
            "source_bank": "rhiannon.bnk",
            "voice_character": adult_voice,
            "reference_sha256": "b" * 64,
            "queue_ids": adult_queue_ids,
        },
        {
            "variant_id": "c" * 64,
            "cluster_id": "child-cluster",
            "character": "Rhiannon",
            "portrait": "11",
            "source_bank": "rhiannon-child.bnk",
            "voice_character": child_voice,
            "reference_sha256": "d" * 64,
            "queue_ids": ["historical:child"],
        },
    ]
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "voices": [
                    {
                        "character": "Rhiannon",
                        "speaker": "rhiannon-v1",
                        "aliases": ["Aderyn"],
                        "references": ["references/rhiannon.wav"],
                    },
                    {
                        "character": adult_voice,
                        "speaker": "adult-v1",
                        "aliases": [],
                        "references": ["references/adult.wav"],
                    },
                    {
                        "character": child_voice,
                        "speaker": "child-v1",
                        "aliases": [],
                        "references": ["references/child.wav"],
                    },
                ],
                SOURCE_REFERENCE_BINDINGS_FIELD: {
                    "schema": SOURCE_REFERENCE_BINDINGS_SCHEMA,
                    "schema_version": SOURCE_REFERENCE_BINDINGS_VERSION,
                    "source_reference_plan_sha256": "e" * 64,
                    "selected_variants": variants,
                    "queue_voice_overrides": dict(sorted(overrides.items())),
                    "queue_voice_overrides_sha256": queue_voice_overrides_sha256(
                        overrides
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def write_player_candidate_manifest(
    root: Path,
    story_index_sha256: str,
    *,
    portrait_image_sha256: str | None = None,
    quality_scores: Sequence[int] = (99, 98),
) -> Path:
    references = root / "references"
    references.mkdir(parents=True, exist_ok=True)
    report = root / "report.json"
    report.write_text('{"candidate_count":2}', encoding="utf-8")
    voices: list[dict[str, object]] = []
    variants: list[dict[str, object]] = []
    for index, quality_score in enumerate(quality_scores, start=1):
        reference = references / f"rhiannon-{index}.wav"
        with wave.open(str(reference), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16_000)
            audio.writeframes((index * 100).to_bytes(2, "little", signed=True) * 1_600)
        variant_id = str(index) * 64
        voice_character = f"Player candidate Rhiannon {index}"
        voices.append(
            {
                "character": voice_character,
                "speaker": f"player-candidate:{variant_id}",
                "references": [f"references/rhiannon-{index}.wav"],
            }
        )
        variants.append(
            {
                "variant_id": variant_id,
                "character": "Rhiannon",
                "portrait": "10",
                "portrait_image_sha256": portrait_image_sha256,
                "source_bank": "rhiannon.bnk",
                "source_voice_ids": [f"play_rhiannon_{index}"],
                "voice_character": voice_character,
                "reference_sha256": sha256_file(reference),
                "source_line_ids": [f"line:source:{index}"],
                "source_excerpts": [
                    {
                        "line_id": f"line:source:{index}",
                        "title": "Voice line",
                        "text": f"Original line {index}.",
                    }
                ],
                "candidate_origin": "story_line_route",
                "source_event_ids": [index],
                "duration_seconds": 3.0 + index,
                "quality_score": quality_score,
            }
        )
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "voices": voices,
                PLAYER_VOICE_CANDIDATES_FIELD: {
                    "schema": "vntts.player-voice-candidates",
                    "schema_version": 4,
                    "story_index_sha256": story_index_sha256,
                    "candidate_report": report.name,
                    "candidate_report_sha256": sha256_file(report),
                    "variants": variants,
                },
            }
        ),
        encoding="utf-8",
    )
    return path

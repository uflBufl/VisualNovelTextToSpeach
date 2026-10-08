"""Checksum-bound sequence replay inputs shared with shutdown tests."""

import hashlib
import json
import wave
from collections.abc import Mapping, Sequence
from pathlib import Path

from PIL import Image, ImageDraw
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import text_sha256, write_generated_audio_manifest
from vntts_artifacts.live_sequence import write_live_sequence_plan


def write_sequence_replay_corpus(
    directory: str | Path,
    *,
    mode: str,
    story_lines: Sequence[dict[str, object]],
    events: Sequence[dict[str, object]],
    dialogue_line_ids: Sequence[str],
    observations: Mapping[str, Sequence[tuple[str | None, str]]] | None = None,
    expected_counts: Mapping[str, int],
    focus_probes: Sequence[bool] = (),
    generated_line_id: str | None = None,
) -> Path:
    root = Path(directory)
    story = root / "story.jsonl"
    story_records: list[dict[str, object]] = [
        {
            "record_type": "metadata",
            "schema": "vntts.story-index",
            "schema_version": 1,
            "line_count": len(story_lines),
            "source_audio_completion": "duration-seconds",
        }
    ]
    story_records.extend(
        {
            "record_type": "line",
            "kind": "dialogue",
            **line,
        }
        for line in story_lines
    )
    story.write_text(
        "\n".join(json.dumps(record) for record in story_records) + "\n",
        encoding="utf-8",
    )
    plan = root / "live-sequence.json"
    write_live_sequence_plan(
        plan,
        {
            "game_id": "replay-test",
            "producer": {"name": "tests", "version": "1"},
            "source_extract_sha256": hashlib.sha256(b"fixture").hexdigest(),
            "chapters": [
                {
                    "chapter": "1",
                    "entry_event_ids": [events[0]["event_id"]],
                    "events": events,
                }
            ],
        },
        story,
    )
    by_id = {line["line_id"]: line for line in story_lines}
    observation_values: Mapping[str, Sequence[tuple[object, object]]] = (
        observations
        or {
            line_id: [(by_id[line_id]["speaker"], by_id[line_id]["text"])]
            for line_id in dialogue_line_ids
        }
    )
    dialogue = []
    for dialogue_index, line_id in enumerate(dialogue_line_ids):
        line = by_id[line_id]
        event_id = next(
            event["event_id"] for event in events if event.get("line_id") == line_id
        )
        frames = []
        for frame_index, (speaker, text) in enumerate(observation_values[line_id]):
            image = Image.new("RGB", (80, 40), "black")
            ImageDraw.Draw(image).rectangle(
                (8 + frame_index, 16, 28 + frame_index, 24),
                fill="white",
            )
            frame = root / f"sequence-{dialogue_index}-{frame_index}.png"
            image.save(frame)
            frames.append(
                {
                    "path": frame.name,
                    "sha256": sha256_file(frame),
                    "observed_character": speaker,
                    "observed_text": text,
                }
            )
        dialogue.append(
            {
                "frames": frames,
                "character": line["speaker"],
                "text": line["text"],
                "event_id": event_id,
                "line_id": line_id,
                "expect_playback": line.get("expect_playback", True),
                "source_audio_status": line.get("source_audio_status", "absent"),
                "source_audio_duration_seconds": line.get(
                    "source_audio_duration_seconds"
                ),
                "expected_source": None
                if not line.get("expect_playback", True)
                else (
                    "generated"
                    if line_id == generated_line_id
                    else "live:replay-live-tts"
                ),
            }
        )
    expected: dict[str, object] = {
        "event_ids": [
            next(
                event["event_id"] for event in events if event.get("line_id") == line_id
            )
            for line_id in dialogue_line_ids
        ],
        "line_ids": list(dialogue_line_ids),
        **expected_counts,
    }
    corpus: dict[str, object] = {
        "schema_version": 2,
        "name": f"Sequence {mode} fixture",
        "dialogue": dialogue,
        "live_sequence": {
            "mode": mode,
            "story_index": {
                "path": story.name,
                "sha256": sha256_file(story),
            },
            "plan": {"path": plan.name, "sha256": sha256_file(plan)},
            "focus_probes": list(focus_probes),
            "expected": expected,
        },
    }
    if generated_line_id is not None:
        generated = root / "sequence-generated.wav"
        with wave.open(str(generated), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24_000)
            output.writeframes(b"\0\0\1\0\0\0")
        generated_manifest = root / "sequence-generated.json"
        generated_line = by_id[generated_line_id]
        write_generated_audio_manifest(
            generated_manifest,
            {"fixture": "sequence-replay"},
            [
                {
                    "line_id": generated_line_id,
                    "text_sha256": text_sha256(generated_line["text"]),
                    "audio": generated.name,
                    "audio_format": "wav-pcm16-mono",
                    "audio_sha256": sha256_file(generated),
                    "sample_rate": 24_000,
                    "sample_count": 3,
                }
            ],
        )
        corpus["generated_audio_manifest"] = {
            "path": generated_manifest.name,
            "sha256": sha256_file(generated_manifest),
        }
    path = root / f"sequence-{mode}.json"
    path.write_text(json.dumps(corpus), encoding="utf-8")
    return path

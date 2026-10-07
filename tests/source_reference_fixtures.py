"""Shared source-reference review inputs for tests and catalog snapshots."""

import hashlib
import json
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from vntts_artifacts.audio import probe_pcm16_mono_wav, write_pcm16_wav

from vntts.authoring.source_reference_quality import (
    QUALITY_REVIEW_SCHEMA,
    QUALITY_REVIEW_VERSION,
)


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
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    payload = b"\x89PNG\r\n\x1a\n"
    payload += chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
    rows = b"\x00" + b"\x7f\x30\x10\xff" * 2
    payload += chunk(b"IDAT", zlib.compress(rows * 2))
    payload += chunk(b"IEND", b"")
    path = root / name
    path.write_bytes(payload)
    return {
        "image": name,
        "image_sha256": hashlib.sha256(payload).hexdigest(),
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

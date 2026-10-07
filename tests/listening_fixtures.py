"""Shared listening report inputs and deterministic playback double."""

import hashlib
import json
import wave
from pathlib import Path

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.pcm_playback import PcmClip, PlaybackSnapshot


class FakePlayback:
    def __init__(self) -> None:
        self.sample_rate = 16_000
        self.channels = 1
        self.token = 0
        self.clip = PcmClip(np.empty((0, 1), dtype=np.float32), self.sample_rate)
        self.position = 0
        self.started = False
        self.playing = False
        self.finished = False
        self.underflowed = False
        self.error: str | None = None
        self.play_calls: list[tuple[PcmClip, int]] = []
        self.pause_calls = 0
        self.closed = False

    def load(self, path: str | Path) -> PcmClip:
        with wave.open(str(path), "rb") as source:
            frames = source.getnframes()
        return PcmClip(np.zeros((frames, 1), dtype=np.float32), self.sample_rate)

    def play(self, clip: PcmClip, *, position_frames: int = 0) -> int:
        self.token += 1
        self.clip = clip
        self.position = position_frames
        self.started = False
        self.playing = True
        self.finished = False
        self.underflowed = False
        self.error = None
        self.play_calls.append((clip, position_frames))
        return self.token

    def finish(self, *, underflowed: bool = False) -> None:
        self.position = self.clip.frames
        self.started = True
        self.playing = False
        self.finished = True
        self.underflowed = underflowed

    def snapshot(self) -> PlaybackSnapshot:
        return PlaybackSnapshot(
            token=self.token,
            position_frames=self.position,
            total_frames=self.clip.frames,
            started=self.started,
            playing=self.playing,
            finished=self.finished,
            underflowed=self.underflowed,
            error=self.error,
        )

    def pause(self) -> None:
        self.pause_calls += 1
        self.playing = False

    def resume(self) -> int:
        self.token += 1
        self.playing = True
        self.started = False
        self.finished = False
        return self.token

    def seek(self, position_frames: int) -> int:
        self.token += 1
        self.position = max(0, min(self.clip.frames, position_frames))
        self.finished = False
        return self.token

    def stop(self) -> None:
        self.token += 1
        self.playing = False
        self.finished = False

    def close(self) -> None:
        self.closed = True


def write_model_reports(root: Path, *, item_count: int = 2) -> list[Path]:
    reports = []
    for model_index, model_id in enumerate(("synthetic/one", "synthetic/two"), start=1):
        samples = []
        for item_index in range(item_count):
            text = f"Shared listening line {item_index} ..."
            if model_index == 2:
                text = text.replace("...", "…")
            audio = root / model_id.replace("/", "-") / f"sample-{item_index}.wav"
            values = np.full(800, model_index * 0.05, dtype=np.float32)
            write_pcm16_wav(audio, values, 16_000)
            samples.append(
                {
                    "id": f"sample-{item_index}",
                    "line_id": f"line-{item_index}",
                    "character": "Voice",
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "audio": str(audio),
                    "audio_sha256": sha256_file(audio),
                }
            )
        report = root / f"report-{model_index}.json"
        report.write_text(
            json.dumps(
                {
                    "schema": "vntts.voice-model-report",
                    "schema_version": 1,
                    "model_id": model_id,
                    "provider": "synthetic",
                    "backend": "synthetic",
                    "model": model_id.rsplit("/", 1)[-1],
                    "samples": samples,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        reports.append(report)
    return reports

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


def write_listening_fixture(root: Path) -> Path:
    session_root = root / "listening-session"
    audio_root = session_root / "audio"
    audio_root.mkdir(parents=True)
    source_audio: dict[str, Path] = {}
    for side, frequency in (("a", 3), ("b", 5)):
        samples = np.sin(np.linspace(0, frequency * np.pi, 800, dtype=np.float32)) * 0.1
        source_audio[side] = root / f"source-{side}.wav"
        write_pcm16_wav(source_audio[side], samples, 16_000)
        write_pcm16_wav(audio_root / f"trial-0001-{side}.wav", samples, 16_000)
    source_report = root / "source-report.json"
    source_report.write_text('{"synthetic": true}\n', encoding="utf-8")
    sources = [
        {"path": str(source_report.resolve()), "sha256": sha256_file(source_report)}
    ]
    source_hash = hashlib.sha256(
        json.dumps(sources, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    key = {
        "schema": "r1999.model-listening-key",
        "schema_version": 1,
        "created_at": "2026-08-15T09:00:00+00:00",
        "source_kind": "model-reports",
        "source_sha256": source_hash,
        "sources": sources,
        "models": [
            {
                "model_id": "provider/model-one",
                "provider": "provider",
                "model": "model-one",
                "reports": ["/legacy/one.json"],
            },
            {
                "model_id": "provider/model-two",
                "provider": "provider",
                "model": "model-two",
                "reports": ["/legacy/two.json"],
            },
        ],
        "assignments": [
            {
                "trial_id": "trial-0001",
                "a": {
                    "model_id": "provider/model-one",
                    "source": str(source_audio["a"].resolve()),
                },
                "b": {
                    "model_id": "provider/model-two",
                    "source": str(source_audio["b"].resolve()),
                },
            }
        ],
    }
    key_path = session_root / ".blind-key.json"
    key_path.write_text(json.dumps(key, sort_keys=True), encoding="utf-8")
    key_path.chmod(0o600)
    session = {
        "schema": "r1999.model-listening-session",
        "schema_version": 1,
        "created_at": "2026-08-15T09:00:00+00:00",
        "updated_at": "2026-08-15T09:01:00+00:00",
        "source_kind": "model-reports",
        "source_sha256": source_hash,
        "blind_key_sha256": sha256_file(key_path),
        "seed": 42,
        "decision_mode": "preference-only",
        "trial_count": 1,
        "completed_count": 1,
        "trials": [
            {
                "trial_id": "trial-0001",
                "queue_id": "legacy:line",
                "line_id": None,
                "text_sha256": "3" * 64,
                "text": "Synthetic line",
                "audio": {
                    "a": "audio/trial-0001-a.wav",
                    "b": "audio/trial-0001-b.wav",
                },
                "rating": {
                    "preference": "a",
                    "reviewed_at": "2026-08-15T09:01:00+00:00",
                },
            }
        ],
    }
    session_path = session_root / "session.json"
    session_path.write_text(json.dumps(session, sort_keys=True), encoding="utf-8")
    report = {
        "schema": "r1999.model-listening-report",
        "schema_version": 1,
        "generated_at": "2026-08-15T09:02:00+00:00",
        "session": str(session_path.resolve()),
        "complete": True,
        "completed_trials": 1,
        "pending_trials": 0,
        "manual_selection_required": True,
        "models": [
            {
                "model_id": "provider/model-one",
                "provider": "provider",
                "model": "model-one",
                "reviewed_trials": 1,
                "preference": {"wins": 1, "losses": 0, "ties": 0, "rate": 1.0},
                "rank": 1,
            },
            {
                "model_id": "provider/model-two",
                "provider": "provider",
                "model": "model-two",
                "reviewed_trials": 1,
                "preference": {"wins": 0, "losses": 1, "ties": 0, "rate": 0.0},
                "rank": 2,
            },
        ],
        "pairwise": [
            {
                "left_model": "provider/model-one",
                "right_model": "provider/model-two",
                "trials": 1,
                "left_wins": 1,
                "right_wins": 0,
                "ties": 0,
            }
        ],
    }
    report_path = session_root / "report.json"
    report_path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    return session_root

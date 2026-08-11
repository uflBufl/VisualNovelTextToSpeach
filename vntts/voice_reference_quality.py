import argparse
import json
import math
import sys
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


class VoiceReferenceQualityError(RuntimeError):
    pass


@dataclass(frozen=True)
class VoiceReferenceMetrics:
    path: str
    duration_seconds: float
    peak_dbfs: float
    rms_dbfs: float
    silence_ratio: float
    leading_silence_seconds: float
    trailing_silence_seconds: float
    clipping_ratio: float
    quality_score: int
    technical_flags: tuple[str, ...]
    music_or_sfx: bool | None = None
    multiple_speakers: bool | None = None

    @property
    def review_complete(self):
        return self.music_or_sfx is not None and self.multiple_speakers is not None

    @property
    def approved(self):
        return (
            not self.technical_flags
            and self.music_or_sfx is False
            and self.multiple_speakers is False
        )


def _decode_pcm(raw, sample_width):
    if sample_width == 1:
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if sample_width == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if sample_width == 3:
        values = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        decoded = values[:, 0] | (values[:, 1] << 8) | (values[:, 2] << 16)
        decoded = np.where(decoded & 0x800000, decoded - 0x1000000, decoded)
        return decoded.astype(np.float32) / 8388608.0
    if sample_width == 4:
        return np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    raise VoiceReferenceQualityError(
        f"Unsupported PCM sample width: {sample_width} bytes"
    )


def read_pcm_wav(path):
    path = Path(path).expanduser().resolve()
    try:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_rate = audio.getframerate()
            sample_width = audio.getsampwidth()
            compression = audio.getcomptype()
            raw = audio.readframes(audio.getnframes())
    except (OSError, wave.Error) as error:
        raise VoiceReferenceQualityError(f"Unable to read WAV {path}: {error}") from error
    if compression != "NONE":
        raise VoiceReferenceQualityError(f"WAV must contain uncompressed PCM: {path}")
    if channels <= 0 or sample_rate <= 0:
        raise VoiceReferenceQualityError(f"WAV has invalid audio metadata: {path}")
    samples = _decode_pcm(raw, sample_width)
    if samples.size % channels:
        raise VoiceReferenceQualityError(f"WAV has an incomplete audio frame: {path}")
    samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sample_rate


def _dbfs(value):
    return 20.0 * math.log10(max(float(value), 1e-9))


def _frame_rms(samples, frame_size):
    if not len(samples):
        return np.array([], dtype=np.float32)
    padding = (-len(samples)) % frame_size
    if padding:
        samples = np.pad(samples, (0, padding))
    frames = samples.reshape(-1, frame_size)
    return np.sqrt(np.mean(np.square(frames), axis=1))


def analyze_voice_reference(path, *, silence_dbfs=-42.0, frame_ms=20):
    samples, sample_rate = read_pcm_wav(path)
    if not len(samples):
        raise VoiceReferenceQualityError(f"WAV contains no audio: {path}")
    duration = len(samples) / sample_rate
    peak = float(np.max(np.abs(samples)))
    rms = float(np.sqrt(np.mean(np.square(samples))))
    frame_size = max(1, round(sample_rate * frame_ms / 1000))
    frame_rms = _frame_rms(samples, frame_size)
    silent = frame_rms <= 10 ** (silence_dbfs / 20.0)
    leading_frames = 0
    for value in silent:
        if not value:
            break
        leading_frames += 1
    trailing_frames = 0
    for value in silent[::-1]:
        if not value:
            break
        trailing_frames += 1
    silence_ratio = float(np.mean(silent)) if len(silent) else 1.0
    clipping_ratio = float(np.mean(np.abs(samples) >= 0.995))

    flags = []
    score = 100
    if duration < 1.5:
        flags.append("too-short")
        score -= 35
    elif duration > 12.0:
        flags.append("too-long")
        score -= 20
    if silence_ratio > 0.35:
        flags.append("excessive-silence")
        score -= min(30, round(silence_ratio * 50))
    if leading_frames * frame_ms / 1000 > 0.5:
        flags.append("long-leading-silence")
        score -= 10
    if trailing_frames * frame_ms / 1000 > 0.5:
        flags.append("long-trailing-silence")
        score -= 10
    if clipping_ratio > 0.001:
        flags.append("clipping")
        score -= min(40, round(clipping_ratio * 1000))
    if _dbfs(rms) < -35.0:
        flags.append("too-quiet")
        score -= 15

    return VoiceReferenceMetrics(
        path=str(Path(path).expanduser().resolve()),
        duration_seconds=round(duration, 3),
        peak_dbfs=round(_dbfs(peak), 2),
        rms_dbfs=round(_dbfs(rms), 2),
        silence_ratio=round(silence_ratio, 4),
        leading_silence_seconds=round(leading_frames * frame_ms / 1000, 3),
        trailing_silence_seconds=round(trailing_frames * frame_ms / 1000, 3),
        clipping_ratio=round(clipping_ratio, 6),
        quality_score=max(0, score),
        technical_flags=tuple(flags),
    )


def write_quality_report(metrics, output):
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "clips": [asdict(item) for item in metrics],
        "review_note": (
            "Set music_or_sfx and multiple_speakers after listening; technical "
            "metrics alone never approve a reference."
        ),
    }
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(output)
    return output


def create_parser():
    parser = argparse.ArgumentParser(
        description="Score WAV files for voice-reference technical quality."
    )
    parser.add_argument("wav", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(arguments=None):
    arguments = create_parser().parse_args(arguments)
    try:
        metrics = [analyze_voice_reference(path) for path in arguments.wav]
        output = write_quality_report(metrics, arguments.output)
    except VoiceReferenceQualityError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Scored {len(metrics)} clip(s) into {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

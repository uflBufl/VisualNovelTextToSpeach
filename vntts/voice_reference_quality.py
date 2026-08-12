import argparse
import json
import math
import sys
import wave
from dataclasses import asdict, dataclass, replace
from itertools import combinations
from pathlib import Path

import numpy as np

from vntts.settings import get_local_data_directory

default_review_path = get_local_data_directory() / "reverse1999" / "clip-reviews.json"


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
        raise VoiceReferenceQualityError(
            f"Unable to read WAV {path}: {error}"
        ) from error
    if compression != "NONE":
        raise VoiceReferenceQualityError(f"WAV must contain uncompressed PCM: {path}")
    if channels <= 0 or sample_rate <= 0:
        raise VoiceReferenceQualityError(f"WAV has invalid audio metadata: {path}")
    samples = _decode_pcm(raw, sample_width)
    if samples.size % channels:
        raise VoiceReferenceQualityError(f"WAV has an incomplete audio frame: {path}")
    samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sample_rate


def _write_pcm_wav(path, samples, sample_rate):
    pcm = np.clip(samples * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(pcm.tobytes())


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


def trim_and_normalize_voice_reference(
    source,
    output,
    *,
    silence_dbfs=-42.0,
    padding_ms=80,
    peak_dbfs=-3.0,
    fade_ms=10,
):
    samples, sample_rate = read_pcm_wav(source)
    if not len(samples):
        raise VoiceReferenceQualityError(f"WAV contains no audio: {source}")
    frame_ms = 10
    frame_size = max(1, round(sample_rate * frame_ms / 1000))
    active = _frame_rms(samples, frame_size) > 10 ** (silence_dbfs / 20.0)
    active_frames = np.flatnonzero(active)
    if not len(active_frames):
        raise VoiceReferenceQualityError(
            f"WAV contains no speech-level audio: {source}"
        )
    padding = round(sample_rate * padding_ms / 1000)
    start = max(0, int(active_frames[0]) * frame_size - padding)
    end = min(len(samples), (int(active_frames[-1]) + 1) * frame_size + padding)
    normalized = samples[start:end].copy()
    peak = float(np.max(np.abs(normalized)))
    if peak <= 1e-9:
        raise VoiceReferenceQualityError(f"WAV contains no audible signal: {source}")
    target_peak = 10 ** (peak_dbfs / 20.0)
    normalized *= target_peak / peak
    fade_samples = min(round(sample_rate * fade_ms / 1000), len(normalized) // 2)
    if fade_samples:
        fade = np.linspace(0.0, 1.0, fade_samples, endpoint=False, dtype=np.float32)
        normalized[:fade_samples] *= fade
        normalized[-fade_samples:] *= fade[::-1]

    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    _write_pcm_wav(temporary, normalized, sample_rate)
    temporary.replace(output)
    return output


def review_voice_reference(metrics, *, music_or_sfx, multiple_speakers):
    if not isinstance(music_or_sfx, bool) or not isinstance(multiple_speakers, bool):
        raise VoiceReferenceQualityError(
            "Music/SFX and multiple-speaker review decisions are required"
        )
    return replace(
        metrics,
        music_or_sfx=music_or_sfx,
        multiple_speakers=multiple_speakers,
    )


def record_clip_review(
    metrics,
    *,
    speaker_name,
    npc_id,
    bank,
    media_id,
    chapter,
    path=default_review_path,
):
    if not metrics.review_complete:
        raise VoiceReferenceQualityError("Listen to and review the clip before saving")
    path = Path(path).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        document = {"version": 1, "clips": []}
    except (OSError, json.JSONDecodeError) as error:
        raise VoiceReferenceQualityError(f"Unable to read clip reviews: {error}")
    clips = document.get("clips")
    if document.get("version") != 1 or not isinstance(clips, list):
        raise VoiceReferenceQualityError("Clip review file has an unsupported format")
    bank_name = Path(bank).name
    clips[:] = [
        item
        for item in clips
        if not (item.get("bank") == bank_name and item.get("media_id") == int(media_id))
    ]
    item = {
        "speaker_name": speaker_name.strip(),
        "npc_id": str(npc_id).strip(),
        "bank": bank_name,
        "media_id": int(media_id),
        "chapter": str(chapter),
        "approved": metrics.approved,
        "metrics": asdict(metrics),
    }
    clips.append(item)
    clips.sort(
        key=lambda value: (
            value["speaker_name"].casefold(),
            value["bank"].casefold(),
            value["media_id"],
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


def select_reference_set(
    clips,
    speaker_name,
    *,
    minimum_clips=3,
    maximum_clips=5,
    minimum_seconds=15.0,
    maximum_seconds=30.0,
):
    approved = [
        item
        for item in clips
        if item.get("approved")
        and str(item.get("speaker_name", "")).casefold() == speaker_name.casefold()
    ]
    best = None
    target_seconds = (minimum_seconds + maximum_seconds) / 2
    for count in range(minimum_clips, min(maximum_clips, len(approved)) + 1):
        for selection in combinations(approved, count):
            total = sum(item["metrics"]["duration_seconds"] for item in selection)
            if not minimum_seconds <= total <= maximum_seconds:
                continue
            score = sum(item["metrics"]["quality_score"] for item in selection)
            ranking = (score, -abs(total - target_seconds))
            if best is None or ranking > best[0]:
                best = ranking, selection
    return list(best[1]) if best is not None else []


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

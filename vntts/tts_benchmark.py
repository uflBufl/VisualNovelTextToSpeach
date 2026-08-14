import argparse
import platform
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, process_time

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import write_pcm16_wav

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on Windows
    resource = None

from vntts.settings import get_local_data_directory
from vntts.speech_backend import (
    ChatterboxNanoVoiceRouterBackend,
    PocketTTSVoiceRouterBackend,
)
from vntts.voices import CharacterVoiceRegistry, find_default_voice_manifest

default_output = get_local_data_directory() / "benchmarks" / "tts"
default_text = "The tide is turning. We should return before the storm arrives."


class DiscardOutputStream:
    def __init__(self):
        self.chunks = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def write(self, chunk):
        self.chunks.append(np.asarray(chunk, dtype=np.float32).reshape(-1))
        return False

    def abort(self):
        return None


class DiscardAudioOutput:
    def __init__(self):
        self.streams = []

    def OutputStream(self, **options):
        del options
        stream = DiscardOutputStream()
        self.streams.append(stream)
        return stream

    def stop(self):
        return None


def _rss_mb():
    if resource is None:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        value *= 1024
    return value / (1024 * 1024)


def write_wav(path, audio, sample_rate):
    return write_pcm16_wav(path, audio, sample_rate)


def create_backend(name, registry, cache_root):
    cache_root = Path(cache_root)
    output = DiscardAudioOutput()
    common = {
        "audio_output": output,
        "persistent_audio_cache_directory": cache_root / "audio",
    }
    if name == "pocket-tts":
        return PocketTTSVoiceRouterBackend(
            registry,
            voice_state_cache_directory=cache_root / "voices",
            **common,
        )
    if name == "chatterbox-nano":
        return ChatterboxNanoVoiceRouterBackend(
            registry,
            conditioning_cache_directory=cache_root / "conditionals",
            **common,
        )
    raise ValueError(f"Unsupported benchmark backend: {name}")


def benchmark_backend(
    backend_name,
    registry,
    characters,
    text,
    output_directory,
    *,
    backend_factory=create_backend,
    clock=perf_counter,
    cpu_clock=process_time,
):
    output_directory = Path(output_directory).expanduser().resolve()
    with TemporaryDirectory() as temporary_directory:
        wall_started = clock()
        cpu_started = cpu_clock()
        backend = backend_factory(backend_name, registry, temporary_directory)
        startup_wall_ms = (clock() - wall_started) * 1000
        startup_cpu_ms = (cpu_clock() - cpu_started) * 1000
        samples = []
        for character in characters:
            conditioning_started = clock()
            backend.prime(character)
            conditioning_ms = (clock() - conditioning_started) * 1000

            generation_started = clock()
            cpu_started = cpu_clock()
            prepared = backend.prepare(character, text)
            if backend.capabilities.streaming:
                backend.play(prepared)
                normalized_text = " ".join(text.split())
                voice = registry.resolve(character)
                voice_key = voice.speaker if voice is not None else "narrator"
                audio = backend.audio_cache.get((voice_key, normalized_text))
                if audio is None:
                    raise RuntimeError("Streaming backend did not cache completed audio")
                first_audio_ms = backend.last_first_audio_ms
            else:
                audio = prepared
                first_audio_ms = (clock() - generation_started) * 1000
            generation_wall_ms = (clock() - generation_started) * 1000
            generation_cpu_ms = (cpu_clock() - cpu_started) * 1000
            duration_seconds = len(audio) / backend.sample_rate

            cached_started = clock()
            cached_prepared = backend.prepare(character, text)
            if backend.capabilities.streaming:
                backend.play(cached_prepared)
            cached_replay_ms = (clock() - cached_started) * 1000

            safe_character = "-".join(character.casefold().split())
            audio_path = write_wav(
                output_directory / f"{backend_name}-{safe_character}.wav",
                audio,
                backend.sample_rate,
            )
            samples.append(
                {
                    "character": character,
                    "text": text,
                    "audio": str(audio_path),
                    "duration_seconds": duration_seconds,
                    "conditioning_ms": conditioning_ms,
                    "first_audio_ms": first_audio_ms,
                    "generation_wall_ms": generation_wall_ms,
                    "generation_cpu_ms": generation_cpu_ms,
                    "realtime_factor": generation_wall_ms / (duration_seconds * 1000),
                    "cached_replay_ms": cached_replay_ms,
                    "speaker_similarity_rating": None,
                    "artifact_rating": None,
                }
            )
        stop = getattr(backend, "stop", None)
        if callable(stop):
            stop()
    return {
        "version": 1,
        "backend": backend_name,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "startup_wall_ms": startup_wall_ms,
        "startup_cpu_ms": startup_cpu_ms,
        "peak_rss_mb": _rss_mb(),
        "samples": samples,
    }


def write_report(report, output_directory):
    output_directory = Path(output_directory).expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    path = output_directory / f"{report['backend']}.json"
    atomic_write_json(path, report)
    return path


def build_parser():
    parser = argparse.ArgumentParser(description="Benchmark a live TTS backend")
    parser.add_argument(
        "--backend",
        required=True,
        choices=("pocket-tts", "chatterbox-nano"),
    )
    parser.add_argument("--character", action="append", dest="characters")
    parser.add_argument("--text", default=default_text)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, default=default_output)
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    manifest = arguments.manifest or find_default_voice_manifest()
    if manifest is None:
        raise SystemExit("No complete voice manifest is available")
    registry = CharacterVoiceRegistry.from_file(manifest)
    characters = arguments.characters or ["Kamuta", "Fatutu"]
    missing = [
        character for character in characters if registry.resolve(character) is None
    ]
    if missing:
        raise SystemExit(f"Voice is not available: {missing[0]}")
    report = benchmark_backend(
        arguments.backend,
        registry,
        characters,
        arguments.text,
        arguments.output,
    )
    report_path = write_report(report, arguments.output)
    print(report_path)
    for sample in report["samples"]:
        print(
            f"{sample['character']}: first audio {sample['first_audio_ms']:.0f} ms, "
            f"RTF {sample['realtime_factor']:.2f}, cache "
            f"{sample['cached_replay_ms']:.1f} ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

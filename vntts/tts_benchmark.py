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

from vntts.cli import cli_error, cli_messages
from vntts.settings import get_local_data_directory
from vntts.speech_backend import (
    ChatterboxNanoVoiceRouterBackend,
    MossTTSVoiceRouterBackend,
    PocketTTSVoiceRouterBackend,
)
from vntts.synthesis import SynthesisRequest
from vntts.versioned_json import read_versioned_json
from vntts.voices import CharacterVoiceRegistry, find_default_voice_manifest

default_output = get_local_data_directory() / "benchmarks" / "tts"
default_text = "The tide is turning. We should return before the storm arrives."
TTS_BENCHMARK_CORPUS_VERSION = 1


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


def create_backend(
    name,
    registry,
    cache_root,
    *,
    model_name=None,
    moss_streaming_first_chunk_frames=None,
    moss_streaming_interval=None,
):
    cache_root = Path(cache_root)
    output = DiscardAudioOutput()
    common = {
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
            audio_output=output,
            **common,
        )
    if name == "moss-tts":
        streaming_options = {
            key: value
            for key, value in {
                "streaming_first_chunk_frames": moss_streaming_first_chunk_frames,
                "streaming_interval": moss_streaming_interval,
            }.items()
            if value is not None
        }
        return MossTTSVoiceRouterBackend(
            registry,
            **({"model_name": str(model_name)} if model_name is not None else {}),
            **streaming_options,
            prompt_cache_directory=cache_root / "prompt-codes",
            **common,
        )
    raise ValueError(f"Unsupported benchmark backend: {name}")


def load_tts_benchmark_corpus(path):
    document = read_versioned_json(
        path,
        schema_version=TTS_BENCHMARK_CORPUS_VERSION,
        document_name="TTS benchmark corpus",
    )
    samples = []
    for index, sample in enumerate(document.get("samples", ()), start=1):
        if not isinstance(sample, dict):
            raise ValueError(f"TTS benchmark sample {index} must be an object")
        character = str(sample.get("character") or "Narrator").strip() or "Narrator"
        text = " ".join(str(sample.get("text") or "").split())
        if not text:
            raise ValueError(f"TTS benchmark sample {index} has no text")
        samples.append(
            {
                "id": str(sample.get("id") or f"sample-{index}"),
                "character": character,
                "text": text,
            }
        )
    if not samples:
        raise ValueError("TTS benchmark corpus has no samples")
    return {
        "name": str(document.get("name") or Path(path).stem),
        "samples": samples,
    }


def benchmark_backend(
    backend_name,
    registry,
    characters,
    text,
    output_directory,
    *,
    benchmark_samples=None,
    corpus_name=None,
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
        work_items = benchmark_samples or (
            {"id": character, "character": character, "text": text}
            for character in characters
        )
        for item in work_items:
            sample_id = str(item.get("id") or item["character"])
            character = item["character"]
            sample_text = item["text"]
            conditioning_started = clock()
            backend.prime(character)
            conditioning_ms = (clock() - conditioning_started) * 1000

            generation_started = clock()
            cpu_started = cpu_clock()
            render = getattr(backend, "render", None)
            render_request = SynthesisRequest(
                voice=character,
                text=sample_text,
                generation_profile=getattr(backend, "generation_profile", "stable"),
            )
            if callable(render):
                rendered = render(render_request).collect()
                audio = rendered.pcm
                first_audio_ms = rendered.timing.first_chunk_ms
                fresh_cache_source = rendered.diagnostics.cache_source
            else:
                prepared = backend.prepare(character, sample_text)
                fresh_cache_source = getattr(prepared, "cache_source", None)
            if backend.capabilities.streaming and not callable(render):
                backend.play(prepared)
                normalized_text = " ".join(sample_text.split())
                voice = registry.resolve(character)
                voice_key = voice.speaker if voice is not None else "narrator"
                audio = backend.audio_cache.get((voice_key, normalized_text))
                if audio is None:
                    audio = getattr(backend, "last_generated_audio", None)
                if audio is None:
                    raise RuntimeError("Streaming backend produced no completed audio")
                first_audio_ms = backend.last_first_audio_ms
            elif not callable(render):
                audio = prepared
                first_audio_ms = (clock() - generation_started) * 1000
            generation_wall_ms = (clock() - generation_started) * 1000
            generation_cpu_ms = (cpu_clock() - cpu_started) * 1000
            duration_seconds = len(audio) / backend.sample_rate
            fresh_underrun = bool(getattr(backend, "last_playback_underrun", False))
            fresh_generation_limited = bool(
                getattr(backend, "last_generation_limited", False)
            )

            cached_started = clock()
            if callable(render):
                memory_rendered = render(render_request).collect()
                memory_cache_source = memory_rendered.diagnostics.cache_source
            else:
                cached_prepared = backend.prepare(character, sample_text)
                memory_cache_source = getattr(cached_prepared, "cache_source", None)
            if backend.capabilities.streaming and not callable(render):
                backend.play(cached_prepared)
            cached_replay_ms = (clock() - cached_started) * 1000
            memory_first_audio_ms = (
                memory_rendered.timing.first_chunk_ms
                if callable(render)
                else getattr(backend, "last_first_audio_ms", None)
            )
            memory_underrun = bool(getattr(backend, "last_playback_underrun", False))
            memory_generation_limited = bool(
                getattr(backend, "last_generation_limited", False)
            )

            persistent_replay_ms = None
            persistent_first_audio_ms = None
            persistent_cache_source = None
            persistent_underrun = None
            persistent_generation_limited = None
            if backend.capabilities.streaming and hasattr(
                backend, "persistent_audio_cache"
            ):
                backend.audio_cache.clear()
                persistent_started = clock()
                if callable(render):
                    persistent_rendered = render(render_request).collect()
                    persistent_cache_source = (
                        persistent_rendered.diagnostics.cache_source
                    )
                else:
                    persistent_prepared = backend.prepare(character, sample_text)
                    persistent_cache_source = getattr(
                        persistent_prepared,
                        "cache_source",
                        None,
                    )
                    backend.play(persistent_prepared)
                persistent_replay_ms = (clock() - persistent_started) * 1000
                persistent_first_audio_ms = (
                    persistent_rendered.timing.first_chunk_ms
                    if callable(render)
                    else getattr(backend, "last_first_audio_ms", None)
                )
                persistent_underrun = bool(
                    getattr(backend, "last_playback_underrun", False)
                )
                persistent_generation_limited = bool(
                    getattr(backend, "last_generation_limited", False)
                )

            safe_character = "-".join(character.casefold().split())
            safe_sample_id = "-".join(sample_id.casefold().split()).replace("/", "-")
            audio_path = write_wav(
                output_directory
                / f"{backend_name}-{safe_character}-{safe_sample_id}.wav",
                audio,
                backend.sample_rate,
            )
            samples.append(
                {
                    "id": sample_id,
                    "character": character,
                    "text": sample_text,
                    "audio": str(audio_path),
                    "duration_seconds": duration_seconds,
                    "conditioning_ms": conditioning_ms,
                    "first_audio_ms": first_audio_ms,
                    "generation_wall_ms": generation_wall_ms,
                    "generation_cpu_ms": generation_cpu_ms,
                    "realtime_factor": generation_wall_ms / (duration_seconds * 1000),
                    "cached_replay_ms": cached_replay_ms,
                    "fresh": {
                        "cache_source": fresh_cache_source,
                        "first_pcm_ms": first_audio_ms,
                        "wall_ms": generation_wall_ms,
                        "realtime_factor": generation_wall_ms
                        / (duration_seconds * 1000),
                        "underrun": fresh_underrun,
                        "generation_limited": fresh_generation_limited,
                    },
                    "memory_cache": {
                        "cache_source": memory_cache_source,
                        "first_pcm_ms": memory_first_audio_ms,
                        "wall_ms": cached_replay_ms,
                        "underrun": memory_underrun,
                        "generation_limited": memory_generation_limited,
                    },
                    "persistent_cache": {
                        "cache_source": persistent_cache_source,
                        "first_pcm_ms": persistent_first_audio_ms,
                        "wall_ms": persistent_replay_ms,
                        "underrun": persistent_underrun,
                        "generation_limited": persistent_generation_limited,
                    },
                    "dialogue_to_first_pcm_ms": (
                        conditioning_ms + first_audio_ms
                        if first_audio_ms is not None
                        else None
                    ),
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
        "corpus": corpus_name,
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
        choices=("pocket-tts", "chatterbox-nano", "moss-tts"),
    )
    parser.add_argument("--character", action="append", dest="characters")
    parser.add_argument("--text", default=default_text)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--model",
        help="Local model path or backend model identifier (recommended offline)",
    )
    parser.add_argument("--moss-first-chunk-frames", type=int)
    parser.add_argument("--moss-streaming-interval", type=float)
    parser.add_argument("--output", type=Path, default=default_output)
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    manifest = arguments.manifest or find_default_voice_manifest()
    if manifest is None:
        return cli_error("No complete voice manifest is available")
    registry = CharacterVoiceRegistry.from_file(manifest)
    corpus = (
        load_tts_benchmark_corpus(arguments.corpus)
        if arguments.corpus is not None
        else None
    )
    characters = (
        sorted({sample["character"] for sample in corpus["samples"]})
        if corpus is not None
        else arguments.characters or ["Kamuta", "Fatutu"]
    )
    missing = [
        character for character in characters if registry.resolve(character) is None
    ]
    if missing:
        return cli_error(f"Voice is not available: {missing[0]}")
    backend_factory = create_backend
    if any(
        value is not None
        for value in (
            arguments.model,
            arguments.moss_first_chunk_frames,
            arguments.moss_streaming_interval,
        )
    ):

        def backend_factory(name, registry, cache):
            return create_backend(
                name,
                registry,
                cache,
                model_name=arguments.model,
                moss_streaming_first_chunk_frames=(arguments.moss_first_chunk_frames),
                moss_streaming_interval=arguments.moss_streaming_interval,
            )

    report = benchmark_backend(
        arguments.backend,
        registry,
        characters,
        arguments.text,
        arguments.output,
        benchmark_samples=corpus["samples"] if corpus is not None else None,
        corpus_name=corpus["name"] if corpus is not None else None,
        backend_factory=backend_factory,
    )
    report_path = write_report(report, arguments.output)
    return cli_messages(
        (
            report_path,
            *(
                f"{sample['character']}: first audio {sample['first_audio_ms']:.0f} ms, "
                f"RTF {sample['realtime_factor']:.2f}, cache "
                f"{sample['cached_replay_ms']:.1f} ms"
                for sample in report["samples"]
            ),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())

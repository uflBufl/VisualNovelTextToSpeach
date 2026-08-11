"""Compare local voice-cloning synthesis latency.

Examples:
  uv run python examples/benchmark_tts_backends.py xtts voice.wav
  uv sync --project backends/chatterbox-nano
  uv run python examples/benchmark_tts_backends.py chatterbox-nano voice.wav
  uv sync --project backends/pocket-tts
  uv run python examples/benchmark_tts_backends.py pocket-tts voice.wav
"""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from time import monotonic

import numpy as np

default_text = (
    "The first sentence should begin quickly. The second should already be ready."
)


@dataclass(frozen=True)
class BenchmarkBackend:
    synthesize: object
    sample_rate: int
    conditioning_seconds: float
    streaming: bool = False


def create_backend(name, reference):
    if name == "pocket-tts":
        from vntts.speech_backend import activate_pocket_tts_runtime

        try:
            activate_pocket_tts_runtime()
            from pocket_tts import TTSModel
        except ImportError as error:
            raise RuntimeError(
                "Pocket TTS is optional; run "
                "`uv sync --project backends/pocket-tts` first"
            ) from error

        model = TTSModel.load_model()
        reference_path = Path(reference).expanduser()
        voice_source = (
            str(reference_path.resolve()) if reference_path.is_file() else reference
        )
        conditioning_started = monotonic()
        voice_state = model.get_state_for_audio_prompt(voice_source)
        conditioning_seconds = monotonic() - conditioning_started

        def synthesize_stream(text):
            return model.generate_audio_stream(voice_state, text)

        return BenchmarkBackend(
            synthesize_stream,
            model.sample_rate,
            conditioning_seconds,
            streaming=True,
        )

    reference = Path(reference).expanduser().resolve()
    if name in {"xtts", "xtts-stream"}:
        from vntts.services.tts_engine import TTSEngine

        engine = TTSEngine(
            model_name="tts_models/multilingual/multi-dataset/xtts_v2",
            language="en",
            speaker_wav=str(reference),
        )
        if name == "xtts":
            return BenchmarkBackend(engine.synthesize, engine.sample_rate, 0.0)

        model = engine.tts.synthesizer.tts_model
        conditioning_started = monotonic()
        gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(
            audio_path=str(reference),
            sound_norm_refs=True,
        )
        conditioning_seconds = monotonic() - conditioning_started
        options = dict(engine.synthesis_options)
        options.pop("split_sentences", None)
        options.pop("sound_norm_refs", None)

        def synthesize_stream(text):
            chunks = model.inference_stream(
                text,
                "en",
                gpt_cond_latent,
                speaker_embedding,
                **options,
            )
            return chunks

        return BenchmarkBackend(
            synthesize_stream,
            engine.sample_rate,
            conditioning_seconds,
            streaming=True,
        )

    try:
        from vntts.assets import ModelAssetManager
        from vntts.speech_backend import (
            activate_chatterbox_runtime,
            select_torch_device,
        )

        ModelAssetManager().configure_huggingface_environment()
        activate_chatterbox_runtime()
        import torch
        from chatterbox.tts_turbo import ChatterboxTurboTTS
    except ImportError as error:
        raise RuntimeError(
            "Chatterbox is optional; run "
            "`uv sync --project backends/chatterbox-nano` first"
        ) from error

    device = select_torch_device(torch)
    load_arguments = {"device": device}
    if name == "chatterbox-nano":
        load_arguments["nano"] = True
    try:
        model = ChatterboxTurboTTS.from_pretrained(**load_arguments)
    except TypeError as error:
        if name != "chatterbox-nano" or "nano" not in str(error):
            raise
        raise RuntimeError(
            "The published chatterbox-tts package does not contain Nano yet; "
            "install the pinned runtime from backends/chatterbox-nano."
        ) from error

    conditioning_started = monotonic()
    model.prepare_conditionals(str(reference))
    conditioning_seconds = monotonic() - conditioning_started

    def synthesize(text):
        return model.generate(text)

    return BenchmarkBackend(synthesize, model.sr, conditioning_seconds)


def audio_sample_count(audio):
    shape = getattr(audio, "shape", None)
    if shape is not None:
        return int(shape[-1])
    return len(audio)


def measure_synthesis(backend, text):
    started = monotonic()
    result = backend.synthesize(text)
    first_audio_seconds = None
    if backend.streaming:
        chunks = []
        for chunk in result:
            if first_audio_seconds is None:
                first_audio_seconds = monotonic() - started
            chunks.append(chunk.detach().cpu().numpy())
        audio = np.concatenate(chunks) if chunks else np.array([], dtype=np.float32)
    else:
        audio = result
    elapsed = monotonic() - started
    if first_audio_seconds is None:
        first_audio_seconds = elapsed
    return audio, elapsed, first_audio_seconds


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "backend",
        choices=(
            "xtts",
            "xtts-stream",
            "chatterbox-nano",
            "chatterbox-turbo",
            "pocket-tts",
        ),
    )
    parser.add_argument("reference")
    parser.add_argument("--text", default=default_text)
    parser.add_argument("--iterations", type=int, default=3)
    arguments = parser.parse_args(argv)
    if arguments.iterations < 1:
        parser.error("--iterations must be positive")

    backend = create_backend(arguments.backend, arguments.reference)
    measurements = []
    first_audio_measurements = []
    audio_seconds = None
    for _iteration in range(arguments.iterations):
        audio, elapsed, first_audio_seconds = measure_synthesis(
            backend,
            arguments.text,
        )
        audio_seconds = audio_sample_count(audio) / backend.sample_rate
        measurements.append(elapsed)
        first_audio_measurements.append(first_audio_seconds)

    print(
        json.dumps(
            {
                "backend": arguments.backend,
                "iterations": arguments.iterations,
                "conditioning_seconds": round(backend.conditioning_seconds, 3),
                "audio_seconds": round(audio_seconds, 3),
                "first_audio_seconds": [
                    round(value, 3) for value in first_audio_measurements
                ],
                "synthesis_seconds": [round(value, 3) for value in measurements],
                "mean_first_audio_seconds": round(
                    mean(first_audio_measurements),
                    3,
                ),
                "mean_synthesis_seconds": round(mean(measurements), 3),
                "mean_realtime_factor": round(mean(measurements) / audio_seconds, 3),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

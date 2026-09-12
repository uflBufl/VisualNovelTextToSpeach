"""User-facing speech identity, shared by setup, preparation and reading."""

import re
from pathlib import Path

from vntts.release_backends import SPEECH_BACKEND_LABELS
from vntts.voices import (
    CharacterVoiceRegistry,
    VoiceManifestError,
    find_voice_assignment,
    pocket_tts_preset_voices,
)


def speech_runtime_label(backend):
    """Describe reported compute placement without guessing from settings."""
    from vntts.generated_audio import GeneratedAudioFallbackBackend

    if isinstance(backend, GeneratedAudioFallbackBackend):
        backend = backend.live_backend
    if backend is None:
        return "Compute: engine not loaded."
    status = getattr(backend, "runtime_status", ...)
    if isinstance(status, str) and status.strip():
        return f"Compute: {status.strip()}"
    if status is None:
        return "Compute: engine not running (or still loading)."
    health = getattr(backend, "health", None)
    if isinstance(health, dict):
        process = getattr(backend, "process", None)
        if process is None or process.poll() is not None:
            return "Compute: engine not running."
        device = health.get("device")
        if isinstance(device, str) and device not in {"", "unknown", "None"}:
            accelerator = health.get("accelerator")
            name = accelerator.get("name") if isinstance(accelerator, dict) else None
            return f"Compute: {device.upper()}" + (
                f" ({name})" if isinstance(name, str) and name else ""
            )
    return "Compute device: unknown (not reported by the engine yet)."


def _engine_model_identity(backend, model=None, *, pocket_cloning=False):
    engine = SPEECH_BACKEND_LABELS.get(backend, backend)
    if backend == "pocket-tts":
        model = (
            "Pocket TTS with voice cloning"
            if pocket_cloning
            else "Pocket TTS preset-only"
        )
    elif backend == "moss-tts":
        from vntts.moss_cpp_backend import moss_cpp_requested

        if moss_cpp_requested(model):
            from vntts.moss_cpp_installation import configured_paths

            model = configured_paths(model)[1].name
        elif not model:
            from vntts.speech_backend import default_moss_tts_model

            model = default_moss_tts_model
    elif not model:
        model = {
            "coqui-xtts": "tts_models/multilingual/multi-dataset/xtts_v2",
            "chatterbox-nano": "Chatterbox Nano (default model)",
        }.get(backend, "Backend default")
    return engine, model


def engine_model_label(backend, model=None, *, pocket_cloning=False, compact=False):
    engine, model = _engine_model_identity(
        backend, model, pocket_cloning=pocket_cloning
    )
    if compact:
        if backend == "moss-tts":
            engine = "MOSS"
        return f"{engine} · {readable_model_name(model)}"
    return f"Engine: {engine}\nModel: {model}"


def speech_configuration_rows(settings, *, narrator=None):
    engine, model = _engine_model_identity(
        settings.speech_backend,
        settings.tts_model,
        pocket_cloning=settings.pocket_gated_model_accepted,
    )
    return (
        ("Narrator", narrator or narrator_voice_label(settings)),
        ("Engine", "MOSS" if settings.speech_backend == "moss-tts" else engine),
        ("Model", readable_model_name(model)),
    )


def compact_runtime_label(message):
    """Hide placement counters, never devices, warnings or fallback reasons."""
    message = re.sub(
        r"\s*\(\d+(?:/\d+)? GPU layers\)|, \d+/\d+ GPU layers", "", message
    )
    return re.sub(r"; auxiliary CPU workers: \d+", "", message)


def readable_model_name(model):
    """Keep known model names readable; retain exact identity in details."""
    if not model:
        return "Default model"
    name = str(model).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    aliases = {
        "MOSS-TTS-Local-Transformer-v1.5-MLX-int8": "Local v1.5 · 8-bit MLX",
        "moss-tts-local-v1.5-mlx-int8": "Local v1.5 · 8-bit MLX",
        "moss-tts-local-1.5-q8_0.gguf": "Local v1.5 · 8-bit GGUF",
        "xtts_v2": "XTTS v2",
    }
    if str(model).startswith("openmoss-cpp:"):
        return "OpenMOSS (exact model in details)"
    return aliases.get(name, name if len(name) <= 70 else "Custom model (see details)")


def playback_labels(source, voice):
    """Summarize the diagnostic source; never substitute live defaults for a WAV."""
    voice = voice.partition("; voice ID: ")[0]
    if "/" in voice or "\\" in voice:
        voice = "Reference: " + voice.replace("\\", "/").rsplit("/", 1)[-1]
    if source.startswith("Generated audio"):
        recorded_voice = "Voice not recorded"
        for part in source.split("; "):
            if part.startswith("source voice: "):
                recorded_voice = part.removeprefix("source voice: ")
        provider = source.partition("Recorded with: ")[2].split("; ")[0]
        model = source.partition("; model: ")[2].split("; ")[0]
        engine = SPEECH_BACKEND_LABELS.get(provider, provider)
        label = "Prepared recording · no generation"
        if engine:
            label += f"\n{engine}"
        if model:
            label += f" · {readable_model_name(model)}"
        return recorded_voice, label
    if source.startswith("Original game audio"):
        return "Original game voice", "Played by the game · no generation"
    if source.startswith("Original game cue"):
        return "See playback details", "Game sound, followed by speech"
    if "memory cache" in source or "persistent cache" in source:
        return voice, "Saved preview or cached speech · no generation"
    if source.startswith("MOSS "):
        return voice, "MOSS · new speech"
    if source in {"Not selected", ""}:
        return voice, "No audio yet"
    return voice, "Audio source: see details"


def narrator_voice_label(settings):
    source = find_voice_assignment(settings.voice_assignments, "Narrator")
    if source and source != "default":
        kind, _, value = source.partition(":")
        if kind == "character" and settings.voice_manifest:
            try:
                voice = CharacterVoiceRegistry.from_file(
                    settings.voice_manifest
                ).resolve_source(source)
            except OSError, VoiceManifestError:
                voice = None
            if voice is not None:
                return voice.source_character or voice.character
        if kind == "character" and value.replace(" ", "").startswith("gamenarrator"):
            return "Selected game voice (saved catalog unavailable)"
        if kind == "character" and value == "narrator":
            return "Prepared pack narrator (identity available after loading)"
        if kind in {"character", "preset"}:
            return value.replace("_", " ").title()
    if settings.tts_speaker_wav:
        return f"Reference: {Path(settings.tts_speaker_wav).name}"
    if settings.speech_backend == "pocket-tts":
        return next(
            (
                value.replace("_", " ").title()
                for value in (settings.narrator_speaker, settings.tts_speaker, "alba")
                if value in pocket_tts_preset_voices
            ),
        )
    if settings.speech_backend == "coqui-xtts":
        return settings.narrator_speaker or settings.tts_speaker or "Not chosen yet"
    return "Not chosen yet"


def speech_configuration_label(settings, *, narrator=None, compact=False):
    return (
        f"Narrator voice: {narrator or narrator_voice_label(settings)}"
        + (" · " if compact else "\n")
        + engine_model_label(
            settings.speech_backend,
            settings.tts_model,
            pocket_cloning=settings.pocket_gated_model_accepted,
            compact=compact,
        )
        + (
            f"\nConfigured model: {settings.tts_model or '(automatic)'}"
            if not compact
            else ""
        )
    )


def reading_policy_label(settings):
    if settings.audio_source_policy == "live-tts-only":
        policy = "Live TTS only; saved recordings are bypassed."
    elif settings.generated_audio_manifest:
        policy = (
            "Original game voices first, then prepared recordings."
            if settings.audio_source_policy == "prefer-game-audio"
            else "Play prepared recordings and original game voices."
        )
        policy += " TTS is used for missing audio."
    else:
        policy = "No prepared audio. Prepare stories, or read with live TTS."
    if settings.force_live_narrator:
        policy += " Narrator override: generate live instead of using its recordings."
    return policy

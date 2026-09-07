"""User-facing speech identity, shared by setup, preparation and reading."""

from pathlib import Path

from vntts.release_backends import SPEECH_BACKEND_LABELS
from vntts.voices import find_voice_assignment, pocket_tts_preset_voices


def engine_model_label(backend, model=None, *, pocket_cloning=False):
    engine = SPEECH_BACKEND_LABELS.get(backend, backend)
    if backend == "pocket-tts":
        model = (
            "Pocket TTS with voice cloning"
            if pocket_cloning
            else "Pocket TTS preset-only"
        )
    elif not model:
        if backend == "moss-tts":
            from vntts.speech_backend import default_moss_tts_model

            model = default_moss_tts_model
        else:
            model = {
                "coqui-xtts": "tts_models/multilingual/multi-dataset/xtts_v2",
                "chatterbox-nano": "Chatterbox Nano (default model)",
            }.get(backend, "Backend default")
    return f"Engine: {engine}\nModel: {model}"


def narrator_voice_label(settings):
    source = find_voice_assignment(settings.voice_assignments, "Narrator")
    if source and source != "default":
        kind, _, value = source.partition(":")
        if kind == "character" and value.startswith("game narrator "):
            return (
                value.removeprefix("game narrator ").rsplit(" ", 1)[0].title()
                + " (game voice)"
            )
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


def speech_configuration_label(settings, *, narrator=None):
    return (
        f"Narrator voice: {narrator or narrator_voice_label(settings)}\n"
        + engine_model_label(
            settings.speech_backend,
            settings.tts_model,
            pocket_cloning=settings.pocket_gated_model_accepted,
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

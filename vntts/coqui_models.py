from dataclasses import dataclass


@dataclass(frozen=True)
class CoquiModelSource:
    repo_id: str
    revision: str
    files: tuple[str, ...]
    checkpoint: str | None


# Repositories follow https://github.com/idiap/coqui-ai-TTS/blob/v0.28.0/TTS/.models.json.
# Pin files from https://huggingface.co/api/models/{repo_id}?blobs=true for offline validation.
COQUI_MODEL_SOURCES: dict[str, CoquiModelSource] = {
    "tts_models/multilingual/multi-dataset/xtts_v2": CoquiModelSource(
        repo_id="tts-hub/XTTS-v2",
        revision="345b56f6fbe25cca7103f7f34e471b8fe8e4945f",
        files=("config.json", "model.pth", "vocab.json", "speakers_xtts.pth"),
        checkpoint=None,
    ),
    "tts_models/multilingual/multi-dataset/xtts_v1.1": CoquiModelSource(
        repo_id="tts-hub/XTTS-v1",
        revision="d0f5995c309d5da600d47ce137a7820bf8202b90",
        files=("config.json", "model.pth", "vocab.json"),
        checkpoint=None,
    ),
    "tts_models/multilingual/multi-dataset/bark": CoquiModelSource(
        repo_id="erogol/bark",
        revision="e60f0e46e8375bda0991dd81f7d92825414f25dc",
        files=("config.json", "text_2.pt", "coarse_2.pt", "fine_2.pt", "tokenizer.pth"),
        checkpoint=None,
    ),
    "tts_models/fa/custom/vits-female": CoquiModelSource(
        repo_id="Kamtera/persian-tts-female-vits",
        revision="a1deb794b324844ff65c020e7a68029d99c9dd62",
        files=("config.json", "best_model_30824.pth"),
        checkpoint="best_model_30824.pth",
    ),
    "voice_conversion_models/multilingual/multi-dataset/openvoice_v1": CoquiModelSource(
        repo_id="myshell-ai/OpenVoice",
        revision="c70fc8b939bd1d8213994ff7c88e32be39708271",
        files=(
            "checkpoints/converter/config.json",
            "checkpoints/converter/checkpoint.pth",
        ),
        checkpoint="checkpoint.pth",
    ),
    "voice_conversion_models/multilingual/multi-dataset/openvoice_v2": CoquiModelSource(
        repo_id="myshell-ai/OpenVoiceV2",
        revision="f36e7edfe1684461a8343844af60babc2efbb727",
        files=("converter/config.json", "converter/checkpoint.pth"),
        checkpoint="checkpoint.pth",
    ),
}

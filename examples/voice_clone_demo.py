import argparse
from pathlib import Path

import torch
from TTS.api import TTS

from vntts.settings import get_local_data_directory

project_root = Path(__file__).resolve().parents[1]
default_reference = project_root / "samples" / "speakers" / "01.wav"
default_output = get_local_data_directory() / "generated" / "xtts-reference-clone.wav"
default_model = "tts_models/multilingual/multi-dataset/xtts_v2"
default_voice_id = "vntts-reference"


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Clone a voice from reference audio with Coqui XTTS-v2.",
    )
    parser.add_argument(
        "--text",
        default="Hello. This voice was cloned from the provided reference audio.",
        help="Text to synthesize.",
    )
    parser.add_argument(
        "--reference",
        type=Path,
        default=default_reference,
        help="Reference WAV used to clone the voice.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output,
        help="Generated WAV path.",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="XTTS language code for the synthesized text.",
    )
    parser.add_argument(
        "--model",
        default=default_model,
        help="Coqui model name.",
    )
    parser.add_argument(
        "--voice-id",
        default=default_voice_id,
        help="Name under which Coqui caches the cloned voice.",
    )
    parser.add_argument(
        "--reuse-cached-voice",
        action="store_true",
        help="Reuse voice-id without reading the reference WAV again.",
    )
    return parser.parse_args()


def select_device():
    # XTTS supports CUDA acceleration. CPU is the portable fallback and is used
    # by the macOS dependency configuration in this project.
    return "cuda" if torch.cuda.is_available() else "cpu"


def main():
    arguments = parse_arguments()
    reference = arguments.reference.expanduser().resolve()
    output = arguments.output.expanduser().resolve()

    if not arguments.reuse_cached_voice and not reference.is_file():
        raise FileNotFoundError(f"Reference audio does not exist: {reference}")

    output.parent.mkdir(parents=True, exist_ok=True)
    device = select_device()
    print(f"Loading {arguments.model} on {device}")
    print("XTTS-v2 and its generated output are licensed for non-commercial use.")
    tts = TTS(model_name=arguments.model).to(device)

    synthesis_arguments = {
        "text": arguments.text,
        "speaker": arguments.voice_id,
        "language": arguments.language,
        "file_path": str(output),
    }
    if arguments.reuse_cached_voice:
        print(f"Reusing cached voice {arguments.voice_id!r}")
    else:
        print(f"Cloning {reference} as {arguments.voice_id!r}")
        # Passing both speaker_wav and a custom speaker name clones the voice and
        # stores its conditioning data for later --reuse-cached-voice runs.
        synthesis_arguments["speaker_wav"] = str(reference)

    tts.tts_to_file(**synthesis_arguments)
    print(f"Generated {output}")


if __name__ == "__main__":
    main()

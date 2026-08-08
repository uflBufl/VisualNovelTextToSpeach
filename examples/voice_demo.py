from TTS.api import TTS

from vntts.services.tts_engine import TTSEngine

tts_models_to_try = [
    "tts_models/multilingual/multi-dataset/xtts_v2",
    "tts_models/en/vctk/vits",
]


def main():
    # List available TTS models.
    print(f"Available TTS models are: {TTS().list_models()}")

    tts = TTSEngine(speaker="p227")
    speakers = tts.tts.speakers
    text = "Hello. This is real time neural text to speech!"

    # List speakers supported by the selected model.
    print(f"Available speakers: {speakers}")

    # When speaker_wav is used, the named speaker argument is not needed.
    # audio = tts.tts.tts(
    #     text=text,
    #     speaker_wav='samples/speakers/01.wav',
    #     language='en',
    # )

    # For a multilingual model, select both its speaker and language:
    # tts = TTSEngine(
    #     model_name='tts_models/multilingual/multi-dataset/xtts_v2',
    #     speaker='Craig Gutsy',
    #     language='en',
    # )

    # To test every speaker exposed by the model:
    # for speaker in speakers:
    #     print(f'{speaker} is speaking now')
    #     tts.speak(text, speaker=speaker)

    # Speaker p227 is available in tts_models/en/vctk/vits.
    tts.speak(text)


if __name__ == "__main__":
    main()

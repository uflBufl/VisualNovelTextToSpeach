import sounddevice as sd
import torch
from TTS.api import TTS


tts_models_to_try = [
    'tts_models/multilingual/multi-dataset/xtts_v2',
    'tts_models/en/vctk/vits',
]


class TTSEngine:
    def __init__(self, model_name='tts_models/en/vctk/vits'):
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        print(f'TTS will be executed on {device}')

        self.tts = TTS(model_name=model_name).to(device)
        self.sample_rate = 22050

    def speak(self, text, speaker):
        audio = self.tts.tts(text, speaker=speaker)
        # For multilingual models such as XTTS, also pass the language:
        # audio = self.tts.tts(text, speaker=speaker, language='en')
        # To mimic a voice from an audio sample:
        # audio = self.tts.tts(
        #     text,
        #     speaker_wav='samples/speakers/01.wav',
        #     language='en',
        # )
        sd.play(audio, self.sample_rate)
        sd.wait()

    def stop(self):
        sd.stop()


def main():
    # List available TTS models.
    print(f'Available TTS models are: {TTS().list_models()}')

    tts = TTSEngine()
    speakers = tts.tts.speakers
    text = 'Hello. This is real time neural text to speech!'

    # List speakers supported by the selected model.
    print(f'Available speakers: {tts.tts.speakers}')

    # When speaker_wav is used, the named speaker argument is not needed.
    # tts.speak(text, '')

    # To test every speaker exposed by the model:
    # for speaker in speakers:
    #     print(f'{speaker} is speaking now')
    #     tts.speak(text, speaker)

    # Speaker p227 is available in tts_models/en/vctk/vits.
    tts.speak(text, 'p227')


if __name__ == '__main__':
    main()

import torch
import sounddevice as sd
from TTS.api import TTS

tts_models_to_try = [
    "tts_models/multilingual/multi-dataset/xtts_v2",
    "tts_models/en/vctk/vits",
]

class TTSEngine:
    def __init__(self, model_name="tts_models/en/vctk/vits"):
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print(f'TTS will be executed on {device}')

        self.tts = TTS(model_name=model_name).to(device)
        self.sample_rate = 22050

    def speak(self, text: str, speaker: str):
        audio = self.tts.tts(text, speaker=speaker)
        # If multilang model (XTTS for example)
        # audio = self.tts.tts(text, speaker=speaker, language='en')
        # To mimic voice
        # audio = self.tts.tts(text, speaker_wav='samples/speakers/01.wav', language='en')
        sd.play(audio, self.sample_rate)
        sd.wait()

    def stop(self):
        sd.stop()

tts_models = TTS().list_models()
# List available TTS models
print(f'Availible TTS models are: {tts_models}')

tts = TTSEngine()
speakers = tts.tts.speakers
text = 'Hello. This is real time neural text to speech!'

# List speakers
print(f'Availible speakers: {speakers}')

# If speaker_wav is used
# tts.speak(text, "")

# To test different speakers
# for speaker in speakers:
#     print(f'{speaker} is speaking now')
#     tts.speak(text, speaker)

# For "tts_models/en/vctk/vits"
tts.speak(text, "p227")

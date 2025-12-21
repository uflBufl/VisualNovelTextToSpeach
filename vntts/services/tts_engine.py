from TTS.api import TTS

import torch
import sounddevice as sd

class TTSEngine:
    def __init__(self, model_name="tts_models/en/vctk/vits"):
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print(f'TTS will be executed on {device}')

        self.tts = TTS(model_name=model_name).to(device)
        self.sample_rate = 22050

    def speak(self, text: str):
        audio = self.tts.tts(text, speaker="p227")
        sd.play(audio, self.sample_rate)
        sd.wait()

    def stop(self):
        sd.stop()
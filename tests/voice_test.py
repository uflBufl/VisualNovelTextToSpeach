from TTS.api import TTS
import sounddevice as sd

class TTSEngine:
    def __init__(self, model_name="tts_models/en/vctk/vits"):
        self.tts = TTS(model_name=model_name)
        self.sample_rate = 22050

    def speak(self, text: str):
        audio = self.tts.tts(text, speaker="p273")
        sd.play(audio, self.sample_rate)
        sd.wait()

    def stop(self):
        sd.stop()

if __name__ == "__main__":
    tts = TTSEngine()
    tts.speak("Hello. This is real time neural text to speech.")

# engine = pyttsx3.init()
# engine.say("Hello, this is pyttsx3 speaking")
# engine.runAndWait()


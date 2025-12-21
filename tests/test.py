import sys
from PySide6.QtWidgets import QApplication, QLabel

app = QApplication(sys.argv)
label = QLabel("Hello PySide6 👋")
label.show()
app.exec()




# from TTS.api import TTS
#
# tts = TTS("tts_models/en/vctk/vits")
# print(tts.speakers)
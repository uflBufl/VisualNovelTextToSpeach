import sys

from PySide6.QtWidgets import QApplication, QLabel


def main():
    app = QApplication(sys.argv)
    label = QLabel("Hello PySide6 👋")
    label.show()
    app.exec()

    # To list the speakers available in a model:
    # from TTS.api import TTS
    # tts = TTS("tts_models/en/vctk/vits")
    # print(tts.speakers)


if __name__ == '__main__':
    main()

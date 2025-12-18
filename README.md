# Text To Speach Generator for Visual Novels

## Tesseract

1. Download and install from link: https://tesseract-ocr.github.io/tessdoc/Installation.html
2. Next put tesseract folder in `PATH` or in code use:
   ```py
   pytesseract.pytesseract.tesseract_cmd = r'<full_path_to_your_tesseract_executable>'
   ```

## eSpeak-NG

To Espeak instalation(necessary for sound output):
- Mac
  ```sh
  brew install espeak-ng
  ```
- Windows and Linux: https://github.com/espeak-ng/espeak-ng/blob/master/docs/guide.md

## Commands

UV installation:
https://docs.astral.sh/uv/getting-started/installation/

To add dependecies:
```sh
uv add <name>
```

To sync dependecies:
```sh
uv lock --refresh
uv sync
```

Activate venv:
```sh
./venv/Scripts/activate
```

Run jupyter notebook:
```sh
uv run --with jupyter jupyter lab
```

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

To run vntts:
```sh
uv run -m vntts
```

### Development

To add dependecies:
```sh
uv add <name>
```

To sync dependecies:
```sh
uv sync
uv lock --refresh
```

Activate venv:
```sh
./venv/Scripts/activate
```

Run jupyter notebook to experement with images and OCR (notebooks are in exps directory):
```sh
uv run --with jupyter jupyter lab
```

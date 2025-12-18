# Text To Speach Generator for Visual Novels

## Instal tesseract
1. Download and install from link: https://tesseract-ocr.github.io/tessdoc/Installation.html
2. Next put tesseract folder in `PATH` or in code use:
   ```py
   pytesseract.pytesseract.tesseract_cmd = r'<full_path_to_your_tesseract_executable>'
   ```

## Commands

To Espeak instalation(necessary for sound output)
```sh
brew install espeak-ng
```

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

Run jupyter notebook:
```sh
uv run --with jupyter jupyter lab
```

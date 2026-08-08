# Visual Novel Text to Speech

Captures a visual novel dialog box, recognizes its text with Tesseract, and
reads it aloud with Coqui TTS.

## Requirements

- Python 3.11
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html)
- [eSpeak-NG](https://github.com/espeak-ng/espeak-ng/blob/master/docs/guide.md)

Put the Tesseract executable in `PATH`, or configure its location in code:

```py
pytesseract.pytesseract.tesseract_cmd = r'<full_path_to_your_tesseract_executable>'
```

Install eSpeak-NG on macOS with:

```sh
brew install espeak-ng
```

Follow the linked eSpeak-NG guide on Windows and Linux. A
[CUDA](https://developer.nvidia.com/cuda-downloads)-compatible GPU is optional;
TTS runs on the CPU when CUDA is unavailable.

## Run

```sh
uv sync
uv run -m vntts
```

Press `Ctrl+Shift+H` to capture and read the current dialog. Set
`VNTTS_HOTKEY` using [pynput hotkey syntax](https://pynput.readthedocs.io/en/latest/keyboard.html#global-hotkeys)
to choose another shortcut:

```sh
VNTTS_HOTKEY='<ctrl>+<alt>+r' uv run -m vntts
```

An invalid value falls back to `Ctrl+Shift+H`. Shortcut presses are ignored
while a dialog is already being processed or spoken.

## Project layout

- `vntts/` - application code
- `tests/` - automated unit tests
- `examples/` - interactive GUI, screenshot, and voice demos
- `exps/` - OCR experimentation notebooks
- `samples/` - sample images and audio

## Development

Add a dependency:

```sh
uv add <name>
```

Synchronize or refresh dependencies:

```sh
uv sync
uv lock --refresh
```

uv creates the virtual environment in `.venv/`. Activation is optional:

```sh
# macOS and Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

Run the tests:

```sh
uv run python -m unittest discover
```

Run an interactive example:

```sh
uv run python examples/gui_demo.py
uv run python examples/screenshot_demo.py
uv run python examples/voice_demo.py
```

Run the OCR notebooks from `exps/`:

```sh
uv run --with jupyter jupyter lab
```

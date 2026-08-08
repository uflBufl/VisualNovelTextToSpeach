# Visual Novel Text to Speech

Captures a visual novel dialog box, recognizes its text with Tesseract, and
reads it aloud with Coqui TTS.

## Requirements

- Python 3.11 (tested; newer versions are not yet verified)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html) with English language data
- [eSpeak-NG](https://github.com/espeak-ng/espeak-ng/blob/master/docs/guide.md)
- A working audio output device

Put the Tesseract executable in `PATH`, or configure its location in code:

```py
pytesseract.pytesseract.tesseract_cmd = r'<path-to-tesseract>'
```

Install the external tools on macOS with:

```sh
brew install tesseract espeak-ng
```

On Debian or Ubuntu:

```sh
sudo apt install tesseract-ocr espeak-ng libportaudio2
```

On Windows, follow the linked Tesseract and eSpeak-NG installation guides and
ensure Tesseract is in `PATH`. PortAudio is bundled with `sounddevice` on
macOS and Windows; other Linux distributions may need an equivalent PortAudio
package. See the [`sounddevice` installation guide](https://python-sounddevice.readthedocs.io/en/0.5.3/installation.html).
A [CUDA](https://developer.nvidia.com/cuda-downloads)-compatible GPU is
optional; TTS runs on the CPU when CUDA is unavailable.

## Platform support and permissions

The application targets macOS, Windows, and Linux desktop sessions using X11.
The verified setup is macOS on Apple silicon with Python 3.11.

- macOS: grant the terminal or packaged application access under **Privacy &
  Security -> [Accessibility](https://support.apple.com/guide/mac-help/mh43185/mac)**
  and **[Screen & System Audio Recording](https://support.apple.com/guide/mac-help/mchld6aa7d23/mac)**,
  then restart it.
- Windows: run the application in an interactive desktop session. No extra
  permission is normally required.
- Linux: run under X11 with `DISPLAY` set. As documented in the
  [`pynput` platform limitations](https://pynput.readthedocs.io/en/latest/limitations.html),
  Wayland through XWayland has limited global-keyboard visibility, so the
  shortcut may not work in native Wayland applications. Headless and SSH
  sessions are not supported.

## Run

```sh
uv sync --no-dev
uv run vntts
```

Press `Ctrl+Shift+H` to capture and read the current dialog. Set
`VNTTS_HOTKEY` using [pynput hotkey syntax](https://pynput.readthedocs.io/en/latest/keyboard.html#global-hotkeys)
to choose another shortcut:

```sh
VNTTS_HOTKEY='<ctrl>+<alt>+r' uv run -m vntts
```

An invalid value falls back to `Ctrl+Shift+H`. Shortcut presses are ignored
while a dialog is already being processed or spoken.

Screenshots are stored in `logs/screenshots/` by default. Set
`VNTTS_SCREENSHOT_DIR` to use another directory:

```sh
VNTTS_SCREENSHOT_DIR='/path/to/screenshots' uv run vntts
```

Screenshots are retained until manually deleted and generated files under
`logs/` are ignored by Git.

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
uv sync --group dev
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
uv sync --no-dev --group notebook
uv run --no-dev --group notebook jupyter lab
```

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

The application targets Windows 11 first and also supports running from source
on macOS. Linux support is limited to X11.

- macOS: grant the terminal or packaged application access under **Privacy &
  Security -> [Accessibility](https://support.apple.com/guide/mac-help/mh43185/mac)**
  and **[Screen & System Audio Recording](https://support.apple.com/guide/mac-help/mchld6aa7d23/mac)**,
  then restart it. Screen-region and selected-window capture are supported.
- Windows: run the application in an interactive desktop session. No extra
  permission is normally required. Select the game under **Settings -> Capture
  source** and use borderless-windowed mode; minimized and exclusive-fullscreen
  windows cannot be captured reliably.
- Linux: run under an X11 desktop session with `DISPLAY` set. Screen-region and
  selected-window capture are supported. Native Wayland, headless, and SSH
  sessions are rejected because reliable global capture and hotkeys are not
  available.

## Run

```sh
uv sync --no-dev
uv run vntts-app
```

The application runs in the system tray. Its settings are stored in the
current user's application-data directory. On first launch, complete the setup
wizard to select the game, verify OCR and audio, calibrate the dialogue area,
and run an OCR-to-speech test. Use **Manage models and voices** in the tray to
download or verify the speech model and import local character voice references.
On Windows, models and imported voice packs are stored under
`%LOCALAPPDATA%\VisualNovelTextToSpeech\`. Run the terminal interface for
development or recovery:

```sh
uv run vntts
```

Press `Ctrl+Shift+H` to capture and read the current dialog. Set
`VNTTS_HOTKEY` using [pynput hotkey syntax](https://pynput.readthedocs.io/en/latest/keyboard.html#global-hotkeys)
to choose another shortcut:

```sh
VNTTS_HOTKEY='<ctrl>+<alt>+r' uv run -m vntts
```

Press `Ctrl+Shift+L` to start or stop live reading while text is appearing.
Set `VNTTS_LIVE_HOTKEY` to change this shortcut. Live mode recognizes the
dialog every 200 ms and queues stable sentences or phrases without waiting for
the whole dialog to finish. One-time reads are ignored while live mode is on.

Use `Ctrl+Shift+P` to pause or resume speech, `Ctrl+Shift+S` to skip the current
line, `Ctrl+Shift+R` to repeat it, and `Ctrl+Shift+X` to clear the queue. These
shortcuts can be changed in Settings. The tray shows the latest recognized
speaker and text.

Tune live reading with `VNTTS_LIVE_INTERVAL_MS`,
`VNTTS_LIVE_STABILITY_FRAMES`, `VNTTS_LIVE_IDLE_FLUSH_MS`, and
`VNTTS_LIVE_MIN_CHUNK_CHARACTERS`. Invalid values use their defaults.
Low-confidence OCR is retried with alternate preprocessing and is not spoken.
Set its acceptance threshold in Settings or with
`VNTTS_OCR_MINIMUM_CONFIDENCE` from 0 to 100. Settings can also retain one copy
of each uncertain frame and its confidence metadata in the application-data
directory for OCR diagnostics.

Screenshots are stored in `logs/screenshots/` by default. Set
`VNTTS_SCREENSHOT_DIR` to use another directory:

```sh
VNTTS_SCREENSHOT_DIR='/path/to/screenshots' uv run vntts
```

Calibrate the dialog region after changing the game resolution or UI layout:

```sh
uv run vntts-calibrate
```

One-time screenshots are retained until manually deleted. Live-mode frames are
not stored. Generated files under `logs/` are ignored by Git.

Provision the locally cached Reverse: 1999 character references:

```sh
uv run python examples/provision_reverse1999_voices.py
```

For the faster English CPU engine, install its isolated runtime once, select
Chatterbox Nano in Settings, and restart the app:

```sh
uv sync --project backends/chatterbox-nano
```

The first Nano start downloads several gigabytes of model assets.

For experimental streaming speech, install its isolated runtime once, select
Pocket TTS in Settings, and restart the app:

```sh
uv sync --project backends/pocket-tts
```

Character voice cloning also requires accepting the model terms at
<https://huggingface.co/kyutai/pocket-tts> and authenticating once with
`uvx hf auth login`.

Import clean story-NPC references from an installed Reverse: 1999 game bank:

```sh
brew install vgmstream  # macOS only
uv run vntts-reverse1999-index
uv run vntts-reverse1999-voice Kamuta
uv run vntts-reverse1999-voice "NPC name" --bank /path/to/english-voice.bnk
```

NPC mappings and approved reference metadata are stored in
`data/reverse1999-npc-catalog.json`.

Extract or convert game audio directly:

```sh
uv run vntts-wwise-extract /path/to/voice.bnk output/ --convert
uv run vntts-audio-convert input.wem output.wav
```

Use XTTS with character-specific voices and a default narrator voice:

```sh
VNTTS_TTS_MODEL='tts_models/multilingual/multi-dataset/xtts_v2' \
VNTTS_TTS_LANGUAGE='en' \
VNTTS_TTS_PROFILE='stable' \
VNTTS_VOICE_MANIFEST='data/reverse1999-voices/manifest.json' \
VNTTS_NARRATOR_SPEAKER='Claribel Dervla' \
uv run vntts
```

Each character voice uses several varied references and is cloned and cached
when that speaker is first detected. XTTS uses `stable` by default. Select
`stable`, `natural`, or `expressive` with `VNTTS_TTS_PROFILE`; invalid profile
names also use `stable`. Unknown speakers use the narrator voice. Downloaded
references under `data/reverse1999-voices/` are ignored by Git.

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

Format and automatically fix lint findings:

```sh
uv run ruff check --fix .
uv run ruff format .
```

Verify formatting, linting, and tests:

```sh
uv run ruff format --check .
uv run ruff check .
uv run python -m unittest discover -s tests
```

Run an interactive example:

```sh
uv run python examples/gui_demo.py
uv run python examples/screenshot_demo.py
uv run python examples/voice_demo.py
uv run python examples/voice_clone_demo.py
```

Run the OCR notebooks from `exps/`:

```sh
uv sync --no-dev --group notebook
uv run --no-dev --group notebook jupyter lab
```

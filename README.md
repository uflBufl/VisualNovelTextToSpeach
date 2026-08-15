# Visual Novel Text to Speech

Captures a visual novel dialog box, recognizes its text with Tesseract, and
reads it aloud with Pocket TTS or another configured speech engine.

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

The application opens a compact control window and can optionally keep running
in the system tray. Its settings are stored in the current user's application-data
directory. Settings, game profiles, OCR corrections, and OCR review metadata
use versioned JSON documents with shared compatibility checks, damaged-file
fallback, and atomic publication. On first launch, complete the setup
wizard to select the game, verify OCR and audio, calibrate the dialogue area,
and run an OCR-to-speech test. Use **Manage models and voices** in the app to
download or verify the speech model and import local character voice references.
After the speech engine is ready, use **Choose voices** to compare candidates
with the same sample text and assign the preferred voice to the narrator or any
character. The character field is editable, so an OCR name that is missing from
the imported manifest can be mapped manually as soon as it appears.
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

Use the control window to start live reading, read once, pause, skip, replay,
or emergency-stop speech. Select **Compact controls** to replace it with a
small always-on-top strip for borderless/fullscreen play; **Full** restores the
main window, and the selected view is used on the next launch. On Windows,
`Ctrl+Shift+P` pauses or resumes,
`Ctrl+Shift+S` skips, `Ctrl+Shift+R` repeats, and `Ctrl+Shift+X` clears the
queue. Shortcuts can be changed in Settings.

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

For the faster English CPU engine, install its isolated runtime once, select
Chatterbox Nano in Settings, and restart the app:

```sh
uv sync --project backends/chatterbox-nano
```

The first Nano start downloads several gigabytes of model assets.

Pocket TTS is the default speech engine. Install its isolated runtime once and
restart the app:

```sh
uv sync --project backends/pocket-tts
```

Character voice cloning also requires accepting the model terms at
<https://huggingface.co/kyutai/pocket-tts> and authenticating once with
`uvx hf auth login`.

MOSS-TTS v1.5 is the high-quality streaming option for Apple Silicon Macs.
Install its isolated MLX runtime once, choose MOSS-TTS in Settings, select a
narrator reference recording, and restart the app:

```sh
uv sync --project backends/moss-tts
```

The default int8 model and audio tokenizer download about 6.8 GB on first use.
VNTTS keeps the model loaded, caches each character reference, and streams
48 kHz stereo audio with a short initial buffer. Enable voice warm-up to move
the first MLX compilation cost to startup. Set `VNTTS_MOSS_MODEL` to use a
compatible local path or Hugging Face model. MOSS-TTS requires macOS on Apple
Silicon; Pocket TTS remains the portable default.

The runtime pins the official `mlx-audio` release. VNTTS carries a guarded
compatibility loader for its int8 MOSS audio tokenizer until the equivalent
quantized-weight handling is available upstream; it disables itself when
native support is detected.

Game-specific extraction lives in a separate repository. An extractor may
produce three local, game-agnostic artifacts for VNTTS:

- a character voice `manifest.json`;
- a versioned `vntts.story-index` JSONL file for chapter detection and likely
  next-speaker preloading;
- an optional versioned `vntts.generated-audio` JSON manifest for verified
  ahead-of-time audio lookup.

Select these artifacts in Settings or store them in a game profile. VNTTS does
not decrypt game configuration, parse engine assets, inspect audio banks, or
distribute extracted game content. The Reverse: 1999 implementation and its
local-only story/voice workflow are in the sibling `reverse1999-extractor`
project.

Use XTTS with character-specific voices and a default narrator voice:

```sh
VNTTS_TTS_MODEL='tts_models/multilingual/multi-dataset/xtts_v2' \
VNTTS_TTS_LANGUAGE='en' \
VNTTS_TTS_PROFILE='stable' \
VNTTS_VOICE_MANIFEST='/path/to/voice-pack/manifest.json' \
VNTTS_STORY_INDEX='/path/to/story-index.jsonl' \
VNTTS_GENERATED_AUDIO_MANIFEST='/path/to/generated-audio.json' \
VNTTS_NARRATOR_SPEAKER='Claribel Dervla' \
uv run vntts
```

Each character voice uses several varied references and is cloned and cached
when that speaker is first detected. XTTS uses `stable` by default. Select
`stable`, `natural`, or `expressive` with `VNTTS_TTS_PROFILE`; invalid profile
names also use `stable`. Unknown speakers use the narrator voice until mapped.
Imported recordings and user manifests are stored under application data.
Manual assignments are stored in the active game profile and take effect
immediately. Pocket TTS exposes its built-in voice catalog in the chooser; XTTS
exposes the speakers reported by the loaded model; imported character voices are
available with every cloning backend. An explicit manual assignment also uses
live synthesis instead of pre-generated story audio, so the selected voice is
honored consistently.
When generated audio is configured, VNTTS uses it only for an exact story line
whose stable line ID and current UTF-8 text SHA-256 match the manifest. Missing,
modified, partial, ambiguous, or speed-incompatible entries fall back to the
selected live speech engine.

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

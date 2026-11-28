# Visual Novel Text to Speech

VNTTS reads visual novel dialogue aloud as you play. It recognizes text from a
game window or screen region and can use live speech, recordings you prepare
locally, or original game audio when a compatible story index is available.

## Start playing

VNTTS targets Windows 10 version 2004 or later and macOS 14 or later. Linux
capture is available on X11 on a best-effort basis. See the
[player guide](docs/playing.md) for platform permissions and capture options.

1. Launch VNTTS, open a dialogue scene, and choose the game under **Capture
   source** on **Reading**.
2. Select **Start reading**. If setup is required, follow **Set up and start**
   to mark the reading areas, preview speech, and save.
3. Return to the game. Use **Reading** or **Compact** to pause or stop. Review
   **Auto advance** before returning to the game if you want to turn pages
   yourself.

Live speech works after setup. To prepare recordings for installed
Reverse: 1999 stories, open **Stories** and follow
[Prepare a story](docs/playing.md#prepare-a-story).

## Developer setup

Install the [source build requirements](docs/development.md#source-build), then
run from the repository root:

```sh
uv run --no-project python scripts/build.py
uv run vntts-app
```

### Test selection

After code changes, start with
`uv run python -m scripts.run_changed_unittests --local`. See
[Development and testing](docs/development.md#test-selection) for branch
validation, CI and focused checks.

## Documentation

- [Player guide](docs/playing.md) for capture, reading controls and prepared audio.
- [Speech engines](docs/speech-engines.md) for models, cloning and runtime setup.
- [Diagnostics](docs/diagnostics.md) for support bundles, OCR and audio checks.
- [Documentation index](docs/README.md) for development, packaging, architecture,
  authoring and research.

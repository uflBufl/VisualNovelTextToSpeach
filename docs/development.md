# Development and testing

## Source build

Install Python 3.14 or newer, [uv](https://docs.astral.sh/uv/), and
[Rust with Cargo](https://doc.rust-lang.org/book/ch01-01-installation.html).
Install Tesseract 5 with its executable, shared library and language data, and
eSpeak-NG. Playback needs an audio output device.

- macOS needs Xcode Command Line Tools for the native capture worker and the
  build toolchain. Use `xcode-select --install` and
  `brew install tesseract espeak-ng`.
- Debian or Ubuntu needs the native build tools and PortAudio. Use
  `sudo apt install build-essential tesseract-ocr espeak-ng libportaudio2`.
- Windows needs PowerShell, the Rust MSVC toolchain, Visual Studio C++ Build
  Tools and a Windows SDK. Put the Tesseract executable in `PATH` and install
  its DLL and language data. See the [Windows Rust prerequisites](https://rust-lang.github.io/rustup/installation/windows-msvc.html).

Run from the repository root:

```sh
uv run --no-project python scripts/build.py
uv run vntts-app
```

The driver synchronizes the locked app dependencies, builds the Rust audio
worker and builds native capture on macOS and Windows. Linux X11 capture uses
python-xlib. Repeat the build after changing native code or updating the
checkout. The command requires installed system tools. The application prepares
[speech runtimes and models](speech-engines.md) separately.

The live OCR service keeps Tesseract language models loaded in a persistent
worker. If library discovery fails, set `VNTTS_TESSERACT_LIBRARY` to the full
path to the shared library. Set `TESSDATA_PREFIX` to select the language-data
folder. See [Diagnostics](diagnostics.md) for native worker overrides.

## Development tools

Development dependencies are opt-in. Install the locked tools and run the same
format, lint and typing commands as CI:

```sh
uv sync --group dev --frozen
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy
```

Mypy covers the production package, scripts and fixture modules configured in
[`pyproject.toml`](../pyproject.toml). Use `uv add` for an intentional dependency
addition. The [dependency audit](../scripts/dependency_audit.py) and
[lock and wheel report](../scripts/dependency_report.py) are separate CI gates.

## Test selection

After code changes, run the local selector first. It compares staged, unstaged
and untracked changes with HEAD.

```sh
uv run python -m scripts.run_changed_unittests --local
```

For branch validation including local edits, omit `--local` to compare with
the merge base of `origin/main`, or use `--base REF`. Add `--list` to inspect
either selection. The selector follows static dependencies and falls back to
the full suite for changes such as shared dependency configuration or an
unsafe mapping. Add focused tests for affected integration or native paths
that static analysis misses. See [Testing principles](testing-principles.md).

Run the complete suite with the repository runner:

```sh
uv run --frozen python scripts/run_ci_unittests.py discover -s tests
```

The runner uses isolated, bounded Qt test shards on macOS and Windows. Linux
CI runs under Xvfb. See [Test runner internals](macos-test-execution.md) and
[CI configuration](../.github/workflows/ci.yml). Device, screen and packaging
qualification have separate [release gates](release-speech-runtime.md).

## UI development

Generate the deterministic Qt catalogue before reviewing cross-window behavior:

```sh
uv run python scripts/render_ui_catalog.py
```

Open `.codex/ui-catalog/index.html`. Pass `--surface voice-editor` to focus on
one surface and its neighbours. Use [Desktop accessibility](desktop-accessibility.md)
for keyboard and focus requirements and [Reader experience](reader-experience.md)
for the intended reading journey.

The [examples](../examples/) contain interactive demos. Launch Jupyter from
[exps](../exps/) with `uv run --group notebook jupyter lab` for the OCR notebook.

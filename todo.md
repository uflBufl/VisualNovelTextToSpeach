# TODO

## Windows application

### P1 - Usable local application

- [x] Add the Windows-oriented application foundation.
  - [x] Put runtime lifecycle operations behind an application controller.
  - [x] Persist typed settings in the user's application-data directory.
  - [x] Add a system-tray entry point with read, live, calibration, settings,
        diagnostics status, and quit controls.
  - [x] Keep the terminal entry point available for development and recovery.

- [x] Capture a selected game window as well as a calibrated screen region.
  - [x] Follow window movement, display scaling, resolution changes, and game restarts.
  - [x] Detect minimized or unavailable windows and recover without restarting the app.
  - [x] Recommend borderless-windowed mode when exclusive fullscreen cannot be captured.

- [x] Add first-run onboarding.
  - [x] Select the game window and calibrate its dialog area.
  - [x] Check Tesseract, audio output, model, and voice-pack availability.
  - [x] Configure hotkeys and complete an OCR-to-speech test.

- [x] Manage models and character voice packs inside the application.
  - [x] Download large models with progress, cancellation, checksums, and retry support.
  - [x] Import local voice references without distributing game audio in the installer.
  - [x] Store models and caches under the user's local application-data directory.

### P2 - Windows distribution

- [ ] Produce a standalone Windows build on Windows.
  - [x] Compare `pyside6-deploy` and PyInstaller and select PyInstaller one-folder
        mode for the Qt, Torch, Coqui, Tesseract, and eSpeak-NG bundle.
  - [ ] Build on Windows and confirm Python, Qt, Tesseract, English language
        data, and eSpeak-NG are present in the portable artifact.
  - [ ] Verify operation without Python, uv, Tesseract, or development tools installed.

- [ ] Create and sign a Windows installer.
  - Add Start Menu shortcuts, optional startup, upgrades, and clean uninstall support.
  - Preserve downloaded models and user settings during application upgrades.
  - Sign the executable and installer for public distribution.

- [ ] Add Windows compatibility and release testing.
  - Cover Windows 11, common GPU vendors, multiple displays, and DPI scaling.
  - Cover windowed, borderless, normal-user, and elevated game processes.
  - Run installation and OCR-to-speech smoke tests on a clean Windows machine.

### P3 - Later platforms

- [x] Port the stable Windows application to macOS.
- [x] Port the stable Windows application to Linux with explicit X11 and Wayland
      capture behavior.

## P1 - High priority

- [x] Make dialog capture and OCR independent of resolution and UI layout.
  - [x] Add a calibration overlay and persist normalized dialog coordinates.
  - [x] Crop speaker and dialog text independently.
  - [x] Preprocess screenshots for contrast, scale, and threshold before OCR.
  - [x] Match uncertain OCR speaker names against the configured voice manifest.
  - [x] Verify `samples/01.jpeg` as Marcus and `samples/02.png` as X.

- [x] Replace the system-wide single-key `h` shortcut with a configurable key combination.
  - Do not capture the screen while the user is typing in another application unintentionally.
  - Run screenshot, OCR, and speech work outside the keyboard callback.

- [x] Convert the files in `tests/` into safe automated tests.
  - Move interactive GUI, screenshot, and voice demos into an `examples/` directory.
  - Protect executable demos with `if __name__ == "__main__":`.
  - Add unit tests for dialogue parsing with OCR and TTS mocked.

- [x] Fix package discovery in `pyproject.toml`.
  - Include the `vntts` package in built distributions.
  - Add a console entry point for starting the application.
  - Verify that the built wheel can run outside the repository checkout.

- [x] Make speaker-name detection preserve uncertain text.
  - Do not assume every first line followed by a blank line is a character name.
  - Cover dialogue with a speaker, narration, multiple paragraphs, blank OCR output, and trailing newlines.

## P2 - Normal priority

- [x] Pass the converted RGB image to Tesseract instead of the raw BGRA screenshot array.

- [x] Make TTS playback settings model-aware.
  - Read the output sample rate from the loaded model.
  - Do not hard-code speaker `p227` for models that use different speakers or no speaker.
  - Support language selection where required by multilingual models.

- [x] Initialize the TTS engine when the application starts rather than during module import.
  - Report model loading progress and startup failures clearly.
  - Keep modules importable without downloading a model or initializing audio.

- [x] Handle runtime failures without terminating the keyboard listener.
  - Report screen-capture, Tesseract, model, and audio errors.
  - Allow a later shortcut invocation to retry.

- [x] Make screenshot storage configurable.
  - Avoid filename collisions when multiple captures happen within one second.
  - Document retention behavior and ignore generated screenshots in Git.

- [x] Read dialog incrementally while its text is still appearing.
  - Detect stable text across OCR frames and queue sentence-sized phrases.
  - Keep OCR capture running while speech is playing.
  - Discard queued phrases when the dialog or speaker changes.

- [x] Add speech queue controls and visible dialog status.
  - [x] Add configurable pause, skip, repeat, and clear-queue hotkeys and tray actions.
  - [x] Keep the latest recognized speaker and text visible in the tray.
  - [x] Interrupt queued, playing, or synthesizing speech when the dialog changes.

- [x] Retry and withhold uncertain OCR results.
  - [x] Score recognized words using Tesseract confidence data.
  - [x] Retry low-confidence frames with alternate preprocessing profiles.
  - [x] Do not queue uncertain text for speech; show its confidence in the tray.
  - [x] Allow the minimum accepted confidence to be configured.
  - [x] Optionally retain uncertain frames for OCR diagnostics.

- [x] Select a Reverse: 1999 voice from the detected speaker name.
  - Load several varied character references from a generated local manifest.
  - Clone and cache each character voice on first use.
  - Provide stable, natural, and expressive synthesis profiles.
  - Use the configured default voice for narration and unknown speakers.

## P3 - Cleanup

- [x] Separate runtime dependencies from notebook and development dependencies.
- [x] Document supported operating systems, Python versions, required permissions, and external tools.
- [x] Add formatting, linting, and automated test commands for contributors.

# TODO

Keep this file limited to actionable work. Remove completed items and empty
sections after their implementation has been verified and committed.

## Windows application

### P2 - Windows distribution

- [ ] Produce a standalone Windows build on Windows.
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

## Next improvements

### P2 - Game profiles and performance

- [ ] Add per-game profiles.
  - Store the selected window, calibrated regions, OCR language, and voice pack.
  - Allow profiles to be selected, duplicated, renamed, and removed.

- [ ] Make live capture adaptive.
  - Reduce capture frequency while the dialog is unchanged or the game is unfocused.
  - Increase capture frequency while text is appearing.
  - Record capture, OCR, and speech latency for performance tuning.

### P3 - OCR vocabulary

- [ ] Add an OCR correction dictionary.
  - Correct recurring character names and game-specific terms.
  - Support global and per-game dictionary entries.
  - Keep automatic corrections visible in diagnostics.

# TODO

Keep this file limited to actionable work. Remove completed items and empty
sections after their implementation has been verified and committed.

## Application usability

### P1 - OCR review

- [ ] Add an OCR learning loop for uncertain screenshots.
  - Review saved screenshots with recognized speaker, text, and confidence.
  - Save corrected phrases globally or for the active game profile.
  - Reload corrections immediately and mark reviewed samples as resolved.

### P1 - Speech controls

- [ ] Add output volume and speaking-speed controls.
- [ ] Add narrator and character voice previews.

### P1 - Dialogue history

- [ ] Add searchable session history with replay and export.

### P2 - Support diagnostics

- [ ] Export a support bundle with sanitized settings, logs, OCR metrics, and
      dependency status.

### P2 - Runtime readiness

- [ ] Warm up the selected model and character voices before gameplay and show
      readiness progress.

### P2 - macOS polish

- [ ] Add launch-at-login control and actionable permission recovery.
- [ ] Add signed and notarized macOS distribution support.

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

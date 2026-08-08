# TODO

## P1 - High priority

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

- [ ] Initialize the TTS engine when the application starts rather than during module import.
  - Report model loading progress and startup failures clearly.
  - Keep modules importable without downloading a model or initializing audio.

- [ ] Handle runtime failures without terminating the keyboard listener.
  - Report screen-capture, Tesseract, model, and audio errors.
  - Allow a later shortcut invocation to retry.

- [ ] Make screenshot storage configurable.
  - Avoid filename collisions when multiple captures happen within one second.
  - Document retention behavior and ignore generated screenshots in Git.

## P3 - Cleanup

- [ ] Separate runtime dependencies from notebook and development dependencies.
- [ ] Document supported operating systems, Python versions, required permissions, and external tools.
- [ ] Add formatting, linting, and automated test commands for contributors.

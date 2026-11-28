# CLI and diagnostics

## Readiness and support

On **Reading**, select **Check readiness** to check capture, OCR and audio.
If a source build reports a missing audio or capture component, follow
[Source build](development.md#source-build). **Support and logs** offers
**Diagnostics** for current pipeline state and **Export report** for a
technical support bundle. The ordinary report includes bounded logs and
generation timelines with redacted local settings paths. It excludes captured
dialogue and screenshots. Review any archive before sharing it.

The diagnostic session recorder is separate. **Record game video and audio**
explicitly opts into sensitive evidence that can include selected game video,
game and generated speech audio, source WAVs and visible or matched dialogue.
Use the saved session details to check which sources were recorded. See the
[macOS native fixture](../scripts/mac_live_recording/README.md) and
[Windows capture worker](../native/capture-worker/windows/README.md) for
platform-specific verification.

For an opt-in timing journal, pass a path that does not exist:

```sh
uv run vntts-app --timeline-journal reading-timeline.jsonl
```

The separate JSONL records symbolic identities and numeric timing. It remains
outside the ordinary support ZIP. Check the acquisition summary in
`generation-timelines.json` for overflow, write errors or a missing terminal
marker before treating the journal as complete. Event receipt does not measure
physical speaker onset.

## Terminal reading

```sh
uv run vntts
```

On Windows and Linux X11, `Ctrl+Shift+H` reads the current dialogue once and
`Ctrl+Shift+L` toggles live reading. Read-once requests are ignored during live
reading. `VNTTS_HOTKEY` and `VNTTS_LIVE_HOTKEY` select other shortcuts in
[pynput syntax](https://pynput.readthedocs.io/en/latest/keyboard.html#global-hotkeys).

For example, to use a locally available XTTS model with a game pack and its
Narrator voice, accept the XTTS model terms in Settings and launch from a
POSIX shell with the matching configuration:

```sh
VNTTS_SPEECH_BACKEND=coqui-xtts \
VNTTS_TTS_MODEL=tts_models/multilingual/multi-dataset/xtts_v2 \
VNTTS_TTS_LANGUAGE=en VNTTS_TTS_PROFILE=stable \
VNTTS_GAME_PACK=game-pack.json VNTTS_NARRATOR_SPEAKER=Narrator \
uv run vntts
```

## OCR and capture

- Change OCR language, **Minimum OCR confidence** and optional **OCR diagnostics**
  in Settings. `VNTTS_OCR_MINIMUM_CONFIDENCE` takes a value from 0 to 100.
  Retained uncertain frames contain game content.
- CLI one-time screenshots can be redirected with `VNTTS_SCREENSHOT_DIR`.
  The app uses its configured screenshot directory. Normal live reading does
  not create a screenshot file for each frame.
- Use `uv run vntts-calibrate` for standalone calibration. Press Enter to
  create a suggested region, move it with arrow keys, resize with Shift plus
  arrow keys, and press Enter to preview the crop. R clears, Escape cancels,
  Ctrl+Return saves and Ctrl+R returns to drawing. If OCR preview fails, the
  explicit save-without-preview option saves the region.
- For controlled live OCR experiments, use `VNTTS_LIVE_INTERVAL_MS`,
  `VNTTS_LIVE_STABILITY_FRAMES`, `VNTTS_LIVE_IDLE_FLUSH_MS` and
  `VNTTS_LIVE_MIN_CHUNK_CHARACTERS`. Use `vntts-benchmark-ocr --help` to
  compare OCR paths with saved images.
- To reproduce recognition and routing from saved frames or collect a real-game
  corpus, follow [Live replay acceptance](live-replay-acceptance.md).

## Native playback and capture

VNTTS starts its Rust audio worker automatically. Set `VNTTS_AUDIO_WORKER` to
select a separately built executable, or `VNTTS_PORTAUDIO_LIBRARY` to select
the installed PortAudio library. Set `VNTTS_CAPTURE_WORKER` to select a
separately built macOS or Windows capture worker. A missing audio worker is
reported as an installation error.

The native worker owns playback for prepared clips, live synthesis,
announcements and previews. Prepared clips transfer their PCM before playback.
Live synthesis supplies a bounded ring buffer, where producer starvation and
device underflows are tracked separately. A device change invalidates the
worker and later playback can open a new one.

With an approved mono WAV and a real output device, compare prepared playback
under ordinary conditions and with artificial Python GIL contention:

```sh
uv run python -m scripts.stress_audio_output approved.wav playback-receipt.json --repeats 2
```

The receipt compares PCM identity, completion, startup and underflows. This
probe measures a controlled playback path. Real gameplay, OCR and background
generation require separate native evidence.

The floating controls and reading warnings are excluded from screen capture
by default. For an opt-in visual test, start the app with
`VNTTS_ALLOW_SCREEN_CAPTURE=1`. For Qt-only screenshots, see the
[screenshot-safe developer tool](../scripts/pyside6-mcp/README.md).

## Reference and render checks

```sh
uv run vntts-check-reference reference.wav --output reference-preflight.json
uv run vntts-benchmark-tts --backend moss-tts --model /path/to/local-model \
  --manifest voices/manifest.json --corpus samples/rhiannon-moss-benchmark.json \
  --output tts-benchmark.json
```

The reference check detects format and gross waveform problems. Speaker
identity, music and pronunciation still need listening. The render benchmark
checks fresh, memory-cache and persistent-cache paths where available. Use a
local model for offline runs. MOSS streaming trials can vary
`--moss-first-chunk-frames` and `--moss-streaming-interval`. The benchmark
opens no audio device, so audio underruns and driver timing remain separate
measurements. See [Synthesis rendering](synthesis-rendering.md).

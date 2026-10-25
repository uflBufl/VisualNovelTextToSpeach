# Native window recorder

This is the tracked replacement for the ignored live-session `capture_helper.swift`.
Requires macOS 15+ SDK, Swift, and existing Screen Recording permission. All capture
commands require an explicit PID and window ID. Missing permission fails at preflight;
there is no prompt, window search heuristic, or desktop fallback. Window removal,
owner/size changes, stream errors, missing video/audio, and encoder/finalization
errors fail the command. Origin-only moves keep the same exact-window filter;
metadata records their count, first/last change and number of omitted intermediate
changes. No wider capture is selected when a window moves. `session.json` remains
`in_progress` after a crash/watchdog and cannot be counted as success.

Compile from the repository root (the fixture has its own entry point):

```sh
xcrun swiftc -parse-as-library scripts/mac_live_recording/recorder.swift scripts/mac_live_recording/capture.swift scripts/mac_live_recording/selfcheck.swift -o /private/tmp/vntts-native-recorder
xcrun swiftc -parse-as-library scripts/mac_live_recording/fixture.swift -o /private/tmp/vntts-recording-fixture
/private/tmp/vntts-native-recorder --selfcheck
/private/tmp/vntts-native-recorder --writer-selfcheck /private/tmp/new-writer-selfcheck
```

For sandboxed compilation, add `-module-cache-path /private/tmp/vntts-swift-cache`.
The selfchecks use only clock/PCM memory and generated pixels; they never
capture a screen or play audio. The writer selfcheck decodes all 180 generated
frames and verifies three scene colors and original PTS. The crash fixture writes
25 seconds of generated video to the same single `window.mov` and calls `_exit`
without finalizing the writer:

```sh
/private/tmp/vntts-native-recorder --writer-crash-selfcheck /private/tmp/new-crash-selfcheck
ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=duration,nb_read_frames /private/tmp/new-crash-selfcheck/window.mov
```

The completed ten-second fragments must retain at least 20 seconds (600 decoded
frames at 30 fps). An incomplete final fragment is expected; neither this file
nor an `in_progress` capture is qualified as a complete session.

The explicitly authorized native component gate opens a nonactivating generated
window, prints its exact PID/window ID, captures **only that window** for 65 seconds,
moves that same window twice without changing its size,
and cleans it up on success, failure, or timeout. The fixture independently exits
at 90 seconds. It does not activate the app, become key, change Spaces, play audio,
or load game assets. Run this only when creating this visible fixture window and
capturing it is authorized:

```sh
uv run python -m scripts.mac_live_recording.run_fixture /private/tmp/vntts-native-recorder /private/tmp/vntts-recording-fixture /private/tmp/new-native-fixture
uv run python -m scripts.mac_live_recording.verify /private/tmp/new-native-fixture
```

The verifier requires ffmpeg/ffprobe. It rejects four-frame/frozen output,
missing target metadata, interrupted/failed capture, drops, middle gaps, shortened
video, and differences between logged and decoded frame counts. It decodes the
beginning/middle/end RGB phases and saves `verified-begin.png`,
`verified-middle.png`, `verified-end.png`. Inspect those images to confirm visible
text changes (`BEGIN`, `MIDDLE`, `END` and advancing seconds). No success may be
inferred from file existence, cleanup, or fabricated frame counts.

For an authorized game recording:

```sh
/private/tmp/vntts-native-recorder --capture GAME_PID VNTTS_PID WINDOW_ID 65 /absolute/new-output
```

Video uses `desktopIndependentWindow`; application audio is captured in separate
`game.caf` and `vntts.caf` streams, with no microphone. Apple's
[audio capture policy](https://developer.apple.com/videos/play/wwdc2022/10155/?time=937)
is application-wide: game audio includes audio from other windows of the game
owning application. It does not isolate one game window's audio. The audio filter for VNTTS
includes only that explicit application; it has no screen output. Raw sample PTS,
Mach display timestamps, clock anchor, selected window ID/PID/bounds/title,
mechanism, selection time, drops and errors are preserved. Captured audio is
digital app output, not physical speaker onset. `--video-only PID WINDOW_ID 65 DIR`
disables audio explicitly. A window whose complete-frame change coverage ends more than five seconds early
gets `status=incomplete` and `video_change_coverage=static_or_incomplete`; it never
claims successful acceptance. Native errors are reported separately in
`native_status`/`errors`. A healthy static window and a stalled stream cannot be
distinguished just from missing content changes, so this is not labeled an encoder
failure. The changing fixture provides the independent evidence needed to qualify
continuous dynamic recording.

SIGINT/SIGTERM request orderly Stop. The capture's requested duration is 1..3600
seconds, native start/stop/finalization are bounded by a duration+15s watchdog,
a separate Stop watchdog is 10s, and the fixture runner has an independent 83s timeout. Watchdog output can be
partial and is never successful evidence. Video uses one continuous MOV with
`movieFragmentInterval=10s`, configured before the writer starts; there is no file
rotation. Both native helpers independently monitor their controlling parent and
available disk space every 250ms. Parent loss, unavailable free-space measurement,
or less than 512 MiB free requests Stop and arms a ten-second forced-exit bound,
including when the Python controller has crashed.

## Four-frame regression

The old helper used `queueDepth=3`, directly appended ScreenCaptureKit's
IOSurface-backed samples, and left H.264 frame reordering at its default `true`.
All 13 prior runs stopped receiving complete video frames after four samples
(~0.1s) while application audio continued for 21..64s. The SDK states that
`AVAssetWriterInput.append` retains samples and that retaining queued capture
frames can stall SCStream. The native generated-window A/B reproduces four frames
with default reordering and continued recording with only
`AVVideoAllowFrameReorderingKey: false`; raising queue depth or making up frames
is unnecessary. Per-work-item autorelease alone did not fix it. Explicit BGRA was
also sufficient in an eight-second probe, but changing capture pixel format or
copying every frame is not needed for this fix.

To reproduce the original regression without changing tracked files, copy the
three recorder Swift files to a temporary directory, replace the single
`AVVideoAllowFrameReorderingKey: false` with `AVVideoAllowFrameReorderingKey: true`
in the temporary `recorder.swift`, compile those copies, and run the same fixture
recipe into a different new output directory. It should report incomplete change coverage;
the normal build must pass the >60s gate. Re-run the component gate after future
encoder, SDK, queue or buffer-lifetime changes.

## Headless Rust worker audio

The ScreenCaptureKit application filter above does not qualify a headless Rust
child's output. Use the separate explicit-process helper alongside exact-window
video. It never selects the parent or global system audio. Requires macOS 14.2+:

```sh
xcrun clang -std=c11 -fobjc-arc -Wall -Wextra -Werror -mmacosx-version-min=14.2 -framework Foundation -framework CoreAudio -framework AudioToolbox -Wl,-sectcreate,__TEXT,__info_plist,scripts/mac_live_recording/process_tap_info.plist scripts/mac_live_recording/process_tap.m -o /private/tmp/vntts-process-tap
/private/tmp/vntts-process-tap --selfcheck
/private/tmp/vntts-process-tap --inspect WORKER_PID /absolute/path/to/vntts-audio-worker
```

Selfcheck uses generated PCM only. Inspect reads the explicit PID's executable,
process start time and HAL PID roundtrip; neither command starts a tap or asks for
permission. Build at a stable path before qualifying permissions. Apple's
[process tap documentation](https://developer.apple.com/documentation/coreaudio/capturing-system-audio-with-core-audio-taps)
requires a separate Audio Capture grant and `NSAudioCaptureUsageDescription`,
embedded by this compile command. Screen Recording consent does not qualify it.
Only when recording is authorized:

```sh
/private/tmp/vntts-process-tap --capture WORKER_PID /absolute/path/to/vntts-audio-worker 65 /absolute/new-output --allow-audio-capture
```

The command can prompt for Audio Capture. A private unmuted tap preserves normal
playback. The native callback copies into a fixed 64-slot PCM queue, with no file
I/O, allocation or locks; the consumer writes `process.caf` and `audio.jsonl`.
Overflow, malformed buffers, discontinuous timestamps, nonfinite PCM, changed
PID/start time/executable/HAL identity, interrupted recording and failed cleanup
fail closed. SIGINT/SIGTERM request cleanup; a duration+15s watchdog leaves an
incomplete session on a native hang. Output directories must be new.

`session.json` records recorder/target identity, format, host-clock timestamps,
peak and errors. Zero PCM is failure; nonzero PCM is only `captured_unqualified`.
It still requires independent source-WAV correlation, full interval/video
coverage, and native permission/device/restart qualification. This tool does not
change Reading completion timers or establish physical speaker onset.

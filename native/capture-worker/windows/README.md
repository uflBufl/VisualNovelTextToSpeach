# Standalone Windows Graphics Capture helper

Requires Windows 10 version 2004 (build 19041) or newer, including Windows 11.
This source-stage helper is not yet integrated into the Python application or
release bundles. Live Windows testing is a separate owner-run check, not a
prerequisite for integration or merging this change.

Build on Windows with Rust's `x86_64-pc-windows-msvc` toolchain and Visual Studio
C++ build tools:

```powershell
.\scripts\build-windows-capture.ps1
```

The executable is staged at
`native/capture-worker/windows/target/release/vntts-capture-worker.exe`. Build dependencies are
pinned in Cargo.lock. Focused portable tests:

```text
cargo test --locked --manifest-path native/capture-worker/windows/Cargo.toml
```

## Transport

Stdin carries UTF-8 JSON lines, including the newline at most 64 KiB each.
A request has `version: 1`, a positive integer `id`, UUID `session_id` and `op`.
An oversized or unterminated line ends the helper with no partial response.
Other invalid requests get a bounded error response, with `id: 0` if no numeric
request ID can be recovered. There is no unsolicited startup message.

- `open` requires `target: {kind: "window", window_id: HWND, pid: PID}` or
  `target: {kind: "display", display_id: HMONITOR}`. Display ID 0 selects the
  primary monitor. Optional `max_fps` defaults to 10, must be an integer between 1 and
  60. Opening a new session closes the previous one first.
- `capture` accepts `timeout_ms` (1 through 10000, default 2000).
- `close` invalidates pixels immediately and waits at most two seconds for the
  native source thread. Stdin EOF performs the same cleanup. A native startup
  timeout (ten seconds) or shutdown timeout reports an error and exits the
  helper. A parent must still impose its own process deadline and kill a
  nonresponsive helper; GPU/COM calls themselves cannot safely be cancelled.

Each response consists of exactly one JSON header plus a newline followed by
exactly `payload_bytes` raw bytes, with no delimiter after the bytes. Headers
have `version`, matching `id`, `ok`, `payload_bytes` and either `result` or
`error: {code, message}`. Never parse the raw pixel payload as a line.

`open` returns `backend`, `session_id` and QPC `clock_now` seconds. `close`
returns an empty result. `capture` returns full native window/display BGRA8
pixels and metadata: `scope`, `session_id`, selected `window_id`/`pid` (null for
displays), `width`, `height`, `row_stride`, `pixel_format`, `content_rect`,
`desktop_bounds`, `frame_sequence`, `source_time`, `acquired_at` and
`layout_epoch`. `content_rect` is the physical client rectangle inside the
returned pixels. Display bounds are diagnostic and do not select window pixels.
Times use QPC seconds; source time comes from WGC SystemRelativeTime. Static
content may return the same frame identity/time, rather than inventing a frame.

## Native behavior and qualification

The source owns an MTA D3D11 device with BGRA support, WGC HWND/explicit HMONITOR
item, and a free-threaded two-frame pool. Cursor capture is disabled; the system
capture border remains enabled by default. The source drains native frames and
keeps one latest owned pixel buffer, with a maximum 32 megapixels / 128 MiB BGRA
payload. It never creates screenshot files or logs pixels. RowPitch and
ContentSize govern CPU copying; geometry changes invalidate pixels before pool
recreation. PMv2 physical DWM/client bounds are checked against native dimensions;
a mismatch is an explicit unsupported-layout error, never a guessed crop.
Closed items, changed process identity, minimize, invalid dimensions and device
errors invalidate cached pixels. Device-loss HRESULT is retained; reopen the
session after the error. A first device-loss failure recreates the native source once; a second loss is explicit. Recovery clears cached pixels and preserves the session sequence, epoch and HRESULT/removal details on failure.

Capture uses RGBA16F scRGB throughout the native pool and staging texture, then
converts linear Rec.709 to SDR sRGB/BGRA8 with fixed global Reinhard peak mapping
`rgb / (max(rgb) + 0.25)` before sRGB encoding. This compresses all channels
together instead of clipping HDR channels independently; 80-nit scRGB white maps
to 0.8 linear SDR. The fixed mapping also applies to SDR and avoids temporal
auto-exposure changes. Alpha is opaque. This is implemented color conversion,
not evidence of HDR display/game correctness. SDR/HDR visual fidelity, OCR,
latency and CPU cost have not been verified on Windows hardware.

Real-game capture, occlusion, volume HUD, fullscreen, lifecycle/DPI transitions,
device failures and packaged execution remain unverified on Windows. There is
no desktop or legacy capture fallback. Build/check/unit-test success alone does
not establish native runtime correctness.

Microsoft references: [screen capture and HDR pipeline](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture),
[HWND item creation](https://learn.microsoft.com/en-us/windows/win32/api/windows.graphics.capture.interop/nf-windows-graphics-capture-interop-igraphicscaptureiteminterop-createforwindow).

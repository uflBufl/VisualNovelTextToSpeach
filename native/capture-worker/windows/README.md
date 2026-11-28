# Standalone Windows Graphics Capture helper

Requires Windows 10 version 2004 (build 19041) or newer, including Windows 11.
The Python application launches this helper automatically for selected-window
and explicit display capture through the common native-capture protocol.

Prepare the source application from the project root with Rust's
`x86_64-pc-windows-msvc` toolchain and Visual Studio C++ build tools installed:

```powershell
uv run --no-project python scripts/build.py
```

This installs Python dependencies and builds both native workers. To rebuild only
the capture helper, use `powershell -File scripts/build-windows-capture.ps1`.
The executable is staged at
`native/capture-worker/windows/target/release/vntts-capture-worker.exe`. Build dependencies are
pinned in Cargo.lock. Focused portable tests:

```text
cargo test --locked --manifest-path native/capture-worker/windows/Cargo.toml
```

## Transport

See the [shared capture protocol](../PROTOCOL.md) for request and frame fields.
Windows attaches the parent-owned file mapping with `OpenFileMappingW` and
`MapViewOfFile`. It closes its view and handle on reopen, error or shutdown.
The Windows worker uses QPC seconds for its clock and WGC SystemRelativeTime
for source timestamps. Its recording mode hands its owned pixels to the
encoder directly.

Opening a new session closes the previous one. Stdin EOF performs the same
cleanup as `close`. The Windows source bounds native startup and teardown.
The parent separately enforces its request deadline and can terminate an
unresponsive worker because GPU and COM calls can block.

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

On an active SDR output, the frame pool and staging texture use BGRA8. The worker
copies the encoded BGR values directly and sets alpha to opaque. It selects the
format for the monitor containing the game window or for the selected display.
Moving between outputs or changing the output color mode can recreate the pool.
HDR and unidentified outputs use RGBA16F scRGB. This path encodes linear Rec.709
as sRGB, mapping 80-nit white to SDR white and scaling brighter pixels by their
peak channel. Both paths produce the same packed BGRA8 transport format.

For clean-package qualification, use the
[Windows release matrix](../../../packaging/windows/README.md). The
[remaining native checks](../../../todo.md) track adverse recording paths.
Portable tests validate the protocol. Interactive acceptance covers real
window capture, geometry changes, device loss and recorder ownership.

Microsoft references: [screen capture and HDR pipeline](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture),
[HWND item creation](https://learn.microsoft.com/en-us/windows/win32/api/windows.graphics.capture.interop/nf-windows-graphics-capture-interop-igraphicscaptureiteminterop-createforwindow).

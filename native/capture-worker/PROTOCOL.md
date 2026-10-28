# Native capture worker protocol v1

A worker owns one selected native source. It reads UTF-8 JSON lines on stdin,
maximum 65,536 bytes including newline. It writes no unsolicited startup message.
Each response is one bounded UTF-8 JSON header line followed immediately by the
stated number of raw payload bytes, with no delimiter after the payload.
Diagnostics belong on stderr. Requests are serialized, with one outstanding request.

Every request has `version: 1`, a positive integer `id`, `op`, and a parent-created
`session_id` in canonical UUID form (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`).
A worker rejects incompatible versions and session identities.

- `open`: `target` is either `{"kind":"window","window_id":123,"pid":456}`
  or `{"kind":"display","display_id":0}`. Display ID 0 means primary display.
  `max_fps` is an integer from 1 through 60, default 10. A window request must capture only that window;
  failure must not substitute a display or other window.
- `capture`: `timeout_ms` is a positive integer, maximum 10,000, default 2,000.
  Return the latest valid native buffer, including an unchanged frame. An old
  source timestamp alone does not prove failure or stale capture.
- `close`: release the source and return `{}`. The owner can also terminate the
  worker on close, cancellation, timeout, protocol failure, or target failure;
  resources must remain scoped to the worker process.

Success header:

```json
{"version":1,"id":1,"ok":true,"payload_bytes":0,"result":{}}
```

Error header (no payload):

```json
{"version":1,"id":1,"ok":false,"payload_bytes":0,"error":{"code":"target_closed","message":"Target closed"}}
```

`open` result contains `backend` (`macos_screencapturekit`,
`windows_graphics_capture`, or `linux_x11_composite`), matching `session_id`, and `clock_now` in worker
monotonic seconds. No permission request or capture occurs in a protocol self-test.

`capture` result contains:

| Field | Meaning |
| --- | --- |
| `backend`, `session_id` | Match the opened source |
| `scope` | `selected_window` or `display` |
| `window_id`, `pid` | Selected window identity; both null for display capture |
| `width`, `height` | Positive integer output pixel dimensions, at most 32 MiPixels |
| `row_stride` | Exactly `width * 4`; rows are packed |
| `pixel_format` | `BGRA8` |
| `content_rect` | Integer `[x,y,width,height]` canonical extent in output pixels: client area on Windows, whole window without shadows on macOS, client area without border on X11; contained in the output buffer |
| `desktop_bounds` | Integer `[x,y,width,height]` for diagnostics, in native desktop coordinates (rounded logical points on macOS); never used as a pixel crop |
| `frame_sequence` | Positive native buffer sequence; can repeat when the source is unchanged, never regresses |
| `source_time` | Native source time in worker monotonic seconds, or null if unavailable |
| `acquired_at` | Acquisition time in worker monotonic seconds |
| `layout_epoch` | Positive, nondecreasing; changes when output size or content rectangle changes |

The binary payload is exactly `width * height * 4` bytes and at most 128 MiB.
The native worker retains at most one latest frame. The Python owner copies bytes
into an immutable frame before handing it to a consumer. Worker clock values are
not Python clock values: a probe records Python request/response times separately.
No part of this protocol establishes that protected, hidden, minimized, or offscreen
content is capturable; native results and live qualification determine that.

The Linux worker runs as `python -m vntts.x11_capture_worker` using the same
protocol. It requires X11 Composite >=0.2 for window capture and RandR for
explicit display capture. On X11, nonzero display IDs are RandR output IDs;
zero selects the primary RandR output. It never substitutes the root drawable
for a selected window. Its synchronous X11 requests run only inside the owned
helper, which the parent terminates on its request deadline or cancellation.

macOS native errors retain `native_domain` and `native_code` in the error
object as well as in its message. Only application connection interruption
allows one stream recreation per capture request under the original deadline.
Windows allows one device-loss source recreation per session; subsequent loss
fails explicitly. Both recreations invalidate cached pixels before replacing
the source and preserve nondecreasing session epochs and frame sequences.

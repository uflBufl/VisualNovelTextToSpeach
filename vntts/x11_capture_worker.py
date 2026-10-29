"""Process-owned X11 Composite source; synchronous X calls die with this helper."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from typing import Any, BinaryIO
from uuid import UUID

from PIL import Image

MAX_LINE = 65_536
MAX_PIXELS = 32 * 1024 * 1024
BACKEND = "linux_x11_composite"


class Failure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def integer(value: Any, minimum: int = 0, maximum: int = 2**53 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise Failure("invalid_request", "Invalid integer field")
    return value


def request(line: bytes) -> dict[str, Any]:
    try:
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError()
        if type(value.get("version")) is not int or value["version"] != 1:
            raise Failure("protocol_version", "Unsupported capture protocol version")
        integer(value.get("id"), 1)
        session = value.get("session_id")
        if not isinstance(session, str) or str(UUID(session)) != session:
            raise ValueError()
        allowed = {"version", "id", "op", "session_id"}
        if value.get("op") == "open":
            allowed |= {"target", "max_fps"}
            integer(value.get("max_fps", 10), 1, 60)
            _validate_target(value.get("target"))
        elif value.get("op") == "capture":
            allowed.add("timeout_ms")
            integer(value.get("timeout_ms", 2000), 1, 10_000)
        elif value.get("op") != "close":
            raise ValueError()
        if set(value) - allowed:
            raise ValueError()
        return value
    except Failure:
        raise
    except TypeError, ValueError, AttributeError:
        raise Failure("invalid_request", "Invalid capture request") from None


def _validate_target(target: Any) -> None:
    if not isinstance(target, dict):
        raise ValueError()
    if target.get("kind") == "window" and set(target) == {
        "kind",
        "window_id",
        "pid",
    }:
        integer(target["window_id"], 1, 2**32 - 1)
        integer(target["pid"], 1, 2**31 - 1)
    elif target.get("kind") == "display" and set(target) == {
        "kind",
        "display_id",
    }:
        integer(target["display_id"], 0, 2**32 - 1)
    else:
        raise ValueError()


def _channel_shifts(masks: tuple[int, int, int], bits: int) -> list[tuple[int, int]]:
    shifts = []
    for mask in masks:
        if mask <= 0 or mask >= 1 << bits:
            raise Failure("unsupported_format", "Invalid TrueColor channel mask")
        shift = (mask & -mask).bit_length() - 1
        maximum = mask >> shift
        if maximum & (maximum + 1):
            raise Failure("unsupported_format", "Noncontiguous TrueColor channel mask")
        shifts.append((shift, maximum))
    if any(masks[a] & masks[b] for a, b in ((0, 1), (0, 2), (1, 2))):
        raise Failure("unsupported_format", "Overlapping TrueColor masks")
    return shifts


def decode_pixels(
    data: bytes,
    width: int,
    height: int,
    bits: int,
    pad: int,
    order: int,
    masks: tuple[int, int, int],
) -> bytes:
    if width <= 0 or height <= 0 or width > MAX_PIXELS // height:
        raise Failure("invalid_size", "X11 frame exceeds pixel limit")
    if bits not in (16, 24, 32) or pad not in (8, 16, 32) or order not in (0, 1):
        raise Failure("unsupported_format", "Unsupported X11 pixel layout")
    stride = ((width * bits + pad - 1) // pad) * (pad // 8)
    if len(data) != stride * height:
        raise Failure("invalid_frame", "Unexpected X11 image byte count")
    shifts = _channel_shifts(masks, bits)
    if masks == (0xFF0000, 0xFF00, 0xFF) and bits in (24, 32):
        raw = (
            ("BGR" if bits == 24 else "BGRX")
            if order == 0
            else ("RGB" if bits == 24 else "XRGB")
        )
        return (
            Image.frombytes("RGB", (width, height), data, "raw", raw, stride)
            .convert("RGBA")
            .tobytes("raw", "BGRA")
        )
    output = bytearray(width * height * 4)
    size = bits // 8
    for row in range(height):
        for column in range(width):
            offset = row * stride + column * size
            pixel = int.from_bytes(
                data[offset : offset + size], "little" if order == 0 else "big"
            )
            red, green, blue = [
                ((pixel & mask) >> shift) * 255 // maximum
                for mask, (shift, maximum) in zip(masks, shifts, strict=True)
            ]
            destination = (row * width + column) * 4
            output[destination : destination + 4] = bytes((blue, green, red, 255))
    return bytes(output)


def process_identity(pid: int) -> str:
    try:
        # comm may contain spaces and parentheses; starttime is field 22 after its final ')'.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except OSError, IndexError:
        raise Failure("identity_changed", "Selected process no longer exists") from None


class Source:
    def __init__(self, target: dict[str, Any], session: str) -> None:
        session_type = os.environ.get("XDG_SESSION_TYPE", "").strip().casefold()
        if session_type == "wayland" or (
            os.environ.get("WAYLAND_DISPLAY") and session_type != "x11"
        ):
            raise Failure("unsupported_platform", "Wayland capture is unsupported")
        from Xlib import X, Xatom
        from Xlib.display import Display
        from Xlib.ext import composite

        self.X = X
        self.cardinal_atom = Xatom.CARDINAL
        self.target = target
        self.session = session
        self.display = Display()
        self.window = None
        self.redirected = False
        self.errors: list[Any] = []
        self.display.set_error_handler(
            lambda error, _request: self.errors.append(error)
        )
        self.epoch = 1
        self.sequence = 0
        self.layout: tuple[int, int, int, int, int, int, int] | None = None
        self.destroyed = False
        try:
            if target["kind"] == "window":
                if not self.display.has_extension("Composite"):
                    raise Failure(
                        "unsupported_capture", "X11 Composite extension is unavailable"
                    )
                version = self.display.composite_query_version()
                if (version.major_version, version.minor_version) < (0, 2):
                    raise Failure(
                        "unsupported_capture", "X11 Composite 0.2 is required"
                    )
                self.window = self.display.create_resource_object(
                    "window", target["window_id"]
                )
                self.root = self.window.get_geometry().root
                self.created = process_identity(target["pid"])
                self.pid_atom = self.display.intern_atom("_NET_WM_PID")
                self.window.change_attributes(event_mask=X.StructureNotifyMask)
                self.window.composite_redirect_window(composite.RedirectAutomatic)
                self.redirected = True
                self.sync()
                self.geometry()
            else:
                self.root = self.display.screen().root
                if not self.display.has_extension("RANDR"):
                    raise Failure(
                        "unsupported_capture",
                        "RandR is required for explicit display capture",
                    )
                self.geometry()
        except BaseException:
            self.close()
            raise

    def sync(self) -> None:
        self.display.sync()
        if self.errors:
            error = self.errors.pop(0)
            self.errors.clear()
            raise Failure(
                "native_error",
                f"X11 request failed ({type(error).__name__}, code {error.code})",
            )

    def events(self) -> bool:
        self.sync()
        changed = False
        while self.display.pending_events():
            event = self.display.next_event()
            if (
                self.window
                and getattr(getattr(event, "window", None), "id", None)
                == self.window.id
            ):
                if event.type in (
                    self.X.MapNotify,
                    self.X.UnmapNotify,
                    self.X.ConfigureNotify,
                    self.X.DestroyNotify,
                ):
                    self.epoch += 1
                    changed = True
                    self.destroyed |= event.type == self.X.DestroyNotify
        if self.destroyed:
            raise Failure("target_closed", "Selected X11 window was destroyed")
        return changed

    def geometry(self) -> tuple[int, int, int, int, int, int, int]:
        if self.window:
            if process_identity(self.target["pid"]) != self.created:
                raise Failure("identity_changed", "Selected process identity changed")
            prop = self.window.get_full_property(self.pid_atom, self.cardinal_atom)
            if (
                prop is None
                or prop.format != 32
                or list(prop.value) != [self.target["pid"]]
            ):
                raise Failure(
                    "identity_changed", "Selected X11 window PID no longer matches"
                )
            attributes = self.window.get_attributes()
            if attributes.map_state != self.X.IsViewable:
                raise Failure("target_unavailable", "Selected X11 window is unmapped")
            extent = self.window.get_geometry()
            position = self.root.translate_coords(self.window, 0, 0)
            return (
                position.x,
                position.y,
                extent.width,
                extent.height,
                extent.border_width,
                attributes.visual,
                extent.depth,
            )
        resources = self.root.xrandr_get_screen_resources_current()
        identifier = (
            self.target["display_id"] or self.root.xrandr_get_output_primary().output
        )
        if not identifier or identifier not in resources.outputs:
            raise Failure(
                "target_unavailable",
                "Selected RandR output or primary output is unavailable",
            )
        output = self.display.xrandr_get_output_info(
            identifier, resources.config_timestamp
        )
        if not output.crtc:
            raise Failure("target_unavailable", "Selected RandR output is inactive")
        crtc = self.display.xrandr_get_crtc_info(
            output.crtc, resources.config_timestamp
        )
        attributes = self.root.get_attributes()
        return (
            crtc.x,
            crtc.y,
            crtc.width,
            crtc.height,
            0,
            attributes.visual,
            self.root.get_geometry().depth,
        )

    def capture(self, timeout_ms: int) -> tuple[dict[str, Any], bytes]:
        deadline = monotonic() + timeout_ms / 1000
        self.events()
        before = self.geometry()
        if self.layout is not None and self.layout != before:
            self.epoch += 1
        self.layout = before
        epoch = self.epoch
        x, y, width, height, border, visual_id, depth = before
        if width <= 0 or height <= 0 or width > MAX_PIXELS // height:
            raise Failure("invalid_size", "X11 frame exceeds pixel limit")
        pixmap = None
        try:
            if self.window:
                pixmap = self.window.composite_name_window_pixmap()
                self.sync()
                drawable, left, top = pixmap, border, border
            else:
                drawable, left, top = self.root, x, y
            image = drawable.get_image(
                left, top, width, height, self.X.ZPixmap, 0xFFFFFFFF
            )
            self.sync()
        finally:
            if pixmap:
                pixmap.free()
        after = self.geometry()
        self.events()
        if before != after or self.epoch != epoch:
            raise Failure(
                "layout_changed", "X11 lifecycle changed while acquiring pixels"
            )
        setup = self.display.display.info
        pixel_format = next(
            (item for item in setup.pixmap_formats if item.depth == depth), None
        )
        visual = next(
            (
                item
                for screen in setup.roots
                for allowed in screen.allowed_depths
                for item in allowed.visuals
                if item.visual_id == visual_id
            ),
            None,
        )
        if (
            visual is None
            or visual.visual_class != self.X.TrueColor
            or pixel_format is None
            or image is None
            or image.depth != depth
        ):
            raise Failure(
                "unsupported_format",
                "X11 capture requires a supported TrueColor visual",
            )
        pixels = decode_pixels(
            image.data,
            width,
            height,
            pixel_format.bits_per_pixel,
            pixel_format.scanline_pad,
            setup.image_byte_order,
            (visual.red_mask, visual.green_mask, visual.blue_mask),
        )
        self.events()
        if self.epoch != epoch or monotonic() >= deadline:
            raise Failure(
                "frame_timeout" if self.epoch == epoch else "layout_changed",
                "X11 capture expired or changed layout",
            )
        self.sequence += 1
        return {
            "backend": BACKEND,
            "scope": "selected_window" if self.window else "display",
            "session_id": self.session,
            "window_id": self.target.get("window_id"),
            "pid": self.target.get("pid"),
            "width": width,
            "height": height,
            "row_stride": width * 4,
            "pixel_format": "BGRA8",
            "content_rect": [0, 0, width, height],
            "desktop_bounds": [x, y, width, height],
            "frame_sequence": self.sequence,
            "source_time": None,
            "acquired_at": monotonic(),
            "layout_epoch": self.epoch,
        }, pixels

    def close(self) -> None:
        try:
            if self.redirected and self.window:
                from Xlib.ext import composite

                self.window.composite_unredirect_window(composite.RedirectAutomatic)
                self.display.flush()
        finally:
            self.display.close()


def _execute_request(
    value: dict[str, Any], source: Source | None, source_type: type[Source]
) -> tuple[Source | None, dict[str, Any], bytes]:
    pixels = b""
    if value["op"] == "open":
        if source:
            raise Failure("already_open", "Capture session is already open")
        source = source_type(value["target"], value["session_id"])
        result = {
            "backend": BACKEND,
            "session_id": value["session_id"],
            "clock_now": monotonic(),
        }
    else:
        if source is None or source.session != value["session_id"]:
            raise Failure("session_mismatch", "No matching capture session")
        if value["op"] == "capture":
            result, pixels = source.capture(value.get("timeout_ms", 2000))
        else:
            source.close()
            source = None
            result = {}
    return source, result, pixels


def run(
    input_stream: BinaryIO, output_stream: BinaryIO, source_type: type[Source] = Source
) -> None:
    source: Source | None = None
    try:
        while True:
            line = input_stream.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE or not line.endswith(b"\n"):
                raise Failure(
                    "invalid_request", "Capture request exceeds bounded framing"
                )
            identifier = 0
            pixels = b""
            try:
                value = request(line)
                identifier = value["id"]
                source, result, pixels = _execute_request(value, source, source_type)
                header = {
                    "version": 1,
                    "id": identifier,
                    "ok": True,
                    "result": result,
                    "payload_bytes": len(pixels),
                }
            except Exception as error:
                code = error.code if isinstance(error, Failure) else "native_error"
                # Exception text from third-party code may contain source or window data.
                message = (
                    str(error)
                    if isinstance(error, Failure)
                    else f"X11 capture failed ({type(error).__name__})"
                )
                header = {
                    "version": 1,
                    "id": identifier,
                    "ok": False,
                    "payload_bytes": 0,
                    "error": {"code": code, "message": message},
                }
                pixels = b""
            output_stream.write(
                json.dumps(header, separators=(",", ":")).encode() + b"\n"
            )
            output_stream.write(pixels)
            output_stream.flush()
            if source is None and header["ok"] and value["op"] == "close":
                return
    finally:
        if source:
            source.close()


def main() -> int:
    parent = os.getppid()

    def watch_parent() -> None:
        while os.getppid() == parent:
            sleep(1)
        os._exit(2)

    Thread(target=watch_parent, daemon=True).start()
    try:
        run(sys.stdin.buffer, sys.stdout.buffer)
    except Failure, BrokenPipeError, OSError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

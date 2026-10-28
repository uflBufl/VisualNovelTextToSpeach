"""Owned native buffers for consumer tests without desktop capture permissions."""

from PIL import Image

from vntts.native_capture import NativeFrame


def native_frame(
    image: Image.Image | None = None,
    *,
    content_rect: tuple[int, int, int, int] | None = None,
    desktop_bounds: tuple[int, int, int, int] = (40, 50, 200, 100),
    window_id: int | None = 100,
    pid: int | None = 42,
    backend: str = "macos_screencapturekit",
) -> NativeFrame:
    image = image if image is not None else Image.new("RGB", (200, 100))
    width, height = image.size
    return NativeFrame(
        backend=backend,
        scope="selected_window" if window_id is not None else "display",
        session_id="12345678-1234-1234-1234-123456789abc",
        window_id=window_id,
        pid=pid,
        width=width,
        height=height,
        row_stride=width * 4,
        pixel_format="BGRA8",
        content_rect=content_rect or (0, 0, width, height),
        desktop_bounds=desktop_bounds,
        frame_sequence=1,
        source_time=1,
        acquired_at=2,
        layout_epoch=1,
        pixels=image.convert("RGBA").tobytes("raw", "BGRA"),
    )

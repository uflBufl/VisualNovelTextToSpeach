import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts.x11_capture_worker import (
    Failure,
    Source,
    decode_pixels,
    process_identity,
    request,
    run,
)

SESSION = "12345678-1234-1234-1234-123456789abc"


class X11CaptureWorkerTests(unittest.TestCase):
    def test_wayland_refused_before_loading_x11_or_opening_a_connection(self):
        for environment in (
            {"XDG_SESSION_TYPE": "wayland"},
            {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
        ):
            with patch.dict("os.environ", environment, clear=True):
                with self.assertRaises(Failure) as failure:
                    Source({"kind": "display", "display_id": 0}, SESSION)
                self.assertEqual(failure.exception.code, "unsupported_platform")

    def test_truecolor_decode_handles_padding_byteorder_and_masks(self):
        for order, data in [
            (0, b"\x00\xf8\xe0\x07\xff\xff\x00\x00"),
            (1, b"\xf8\x00\x07\xe0\xff\xff\x00\x00"),
        ]:
            self.assertEqual(
                decode_pixels(data, 1, 2, 16, 32, order, (0xF800, 0x7E0, 0x1F)),
                b"\x00\x00\xff\xff\xff\xff\xff\xff",
            )
        self.assertEqual(
            decode_pixels(
                b"\x03\x02\x01\x00", 1, 1, 24, 32, 0, (0xFF0000, 0xFF00, 0xFF)
            ),
            b"\x03\x02\x01\xff",
        )
        self.assertEqual(
            decode_pixels(
                b"\x00\x01\x02\x03", 1, 1, 32, 32, 1, (0xFF0000, 0xFF00, 0xFF)
            ),
            b"\x03\x02\x01\xff",
        )

    def test_rejects_unsupported_or_unbounded_pixel_layout(self):
        for args in [
            (b"", 0, 1, 32, 32, 0, (1, 2, 4)),
            (b"", 2**30, 2, 32, 32, 0, (1, 2, 4)),
            (b"\x00" * 4, 1, 1, 32, 32, 0, (0x55, 2, 4)),
            (b"\x00" * 4, 1, 1, 32, 32, 0, (1, 1, 4)),
            (b"", 1, 1, 32, 32, 0, (1, 2, 4)),
        ]:
            with self.assertRaises(Failure):
                decode_pixels(*args)

    def test_request_rejects_unknown_fields_and_noninteger_identity(self):
        base = {"version": 1, "id": 1, "op": "capture", "session_id": SESSION}
        self.assertEqual(request(json.dumps(base).encode()), base)
        for field, value in [
            ("id", True),
            ("timeout_ms", 10_001),
            ("session_id", "wrong"),
            ("target", {}),
        ]:
            with self.assertRaises(Failure):
                request(json.dumps({**base, field: value}).encode())
        with self.assertRaises(Failure):
            request(
                json.dumps(
                    {
                        **base,
                        "op": "open",
                        "target": {"kind": "display", "display_id": True},
                    }
                ).encode()
            )

    def test_process_starttime_allows_spaces_and_parentheses_in_name(self):
        with patch(
            "vntts.x11_capture_worker.Path.read_text",
            return_value="42 (game (name)) " + " ".join(["S"] + ["0"] * 18 + ["12345"]),
        ):
            self.assertEqual(process_identity(42), "12345")

    def test_protocol_payload_and_session_ownership(self):
        class FakeSource:
            def __init__(self, target, session):
                self.session = session
                self.close = Mock()
                sources.append(self)

            def capture(self, timeout):
                return {"width": 1}, b"\x00\n\xff\x00"

        sources = []
        values = [
            {"op": "open", "target": {"kind": "display", "display_id": 0}},
            {"op": "close", "session_id": "00000000-0000-0000-0000-000000000000"},
            {"op": "capture"},
            {"op": "close"},
        ]
        wire = b"".join(
            json.dumps(
                {"version": 1, "id": index, "session_id": SESSION, **value}
            ).encode()
            + b"\n"
            for index, value in enumerate(values, 1)
        )
        output = io.BytesIO()
        run(io.BytesIO(wire), output, FakeSource)
        response = io.BytesIO(output.getvalue())
        self.assertTrue(json.loads(response.readline())["ok"])
        self.assertEqual(
            json.loads(response.readline())["error"]["code"], "session_mismatch"
        )
        header = json.loads(response.readline())
        self.assertEqual(header["payload_bytes"], 4)
        self.assertEqual(response.read(4), b"\x00\n\xff\x00")
        self.assertTrue(json.loads(response.readline())["ok"])
        sources[0].close.assert_called_once()

    def test_framing_limit_terminates_without_unbounded_read(self):
        with self.assertRaises(Failure):
            run(io.BytesIO(b"x" * 65_537), io.BytesIO())

    def source(self):
        source = object.__new__(Source)
        source.epoch, source.sequence, source.layout = 1, 0, None
        source.session, source.target = (
            SESSION,
            {"kind": "window", "window_id": 1, "pid": 42},
        )
        source.X = SimpleNamespace(ZPixmap=2, TrueColor=4)
        source.events = Mock(return_value=False)
        source.geometry = Mock(return_value=(20, 30, 1, 1, 1, 7, 24))
        source.sync = Mock()
        pixmap = Mock()
        pixmap.get_image.return_value = SimpleNamespace(
            depth=24, data=b"\x03\x02\x01\x00"
        )
        source.window = Mock()
        source.window.composite_name_window_pixmap.return_value = pixmap
        visual = SimpleNamespace(
            visual_id=7,
            visual_class=4,
            red_mask=0xFF0000,
            green_mask=0xFF00,
            blue_mask=0xFF,
        )
        info = SimpleNamespace(
            image_byte_order=0,
            pixmap_formats=[
                SimpleNamespace(depth=24, bits_per_pixel=32, scanline_pad=32)
            ],
            roots=[SimpleNamespace(allowed_depths=[SimpleNamespace(visuals=[visual])])],
        )
        source.display = SimpleNamespace(display=SimpleNamespace(info=info))
        return source, pixmap

    def test_names_and_frees_pixmap_every_capture_excluding_border(self):
        source, pixmap = self.source()
        for index in (1, 2):
            metadata, pixels = source.capture(1000)
            self.assertEqual(metadata["frame_sequence"], index)
            self.assertEqual(metadata["content_rect"], [0, 0, 1, 1])
            self.assertEqual(pixels, b"\x03\x02\x01\xff")
        self.assertEqual(source.window.composite_name_window_pixmap.call_count, 2)
        self.assertEqual(pixmap.free.call_count, 2)
        pixmap.get_image.assert_called_with(1, 1, 1, 1, 2, 0xFFFFFFFF)

    def test_lifecycle_change_rejects_frame_even_with_same_dimensions(self):
        source, pixmap = self.source()
        # A lifecycle notification between GetImage and publication invalidates its epoch.
        source.events.side_effect = lambda: setattr(source, "epoch", source.epoch + 1)
        with self.assertRaises(Failure) as failure:
            source.capture(1000)
        self.assertEqual(failure.exception.code, "layout_changed")
        pixmap.free.assert_called_once()


if __name__ == "__main__":
    unittest.main()

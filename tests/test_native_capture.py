"""A pipe peer checks the ownership boundary without using screen permissions."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from vntts.native_capture import (
    CaptureError,
    NativeCaptureSession,
    probe_capture_worker,
    worker_command,
    worker_executable,
)
from vntts.runtime_preparation import RuntimePreparationCancelled

PEER = r"""
import json, sys, time
from multiprocessing.shared_memory import SharedMemory
shared = None
mode = sys.argv[1]
count = 0
for line in sys.stdin.buffer:
    q = json.loads(line)
    if mode == "hang-open" or (mode == "hang" and q["op"] == "capture"):
        time.sleep(30)
    if q["op"] == "open":
        assert q["version"] == 2
        shared = SharedMemory(name=q["frame_buffer"]["name"], track=False)
        result = dict(backend="macos_screencapturekit", session_id=q["session_id"], clock_now=500.)
        if mode == "wrong-session-open": result["session_id"] = "unrelated"
        data = b""
    else:
        count += 1
        target = q["session_id"]
        result = dict(backend="macos_screencapturekit", scope="selected_window", session_id=target,
            window_id=17, pid=29, width=2, height=1, row_stride=8, pixel_format="BGRA8",
            content_rect=[0,0,2,1], desktop_bounds=[100,200,200,100],
            frame_sequence=1, source_time=1., acquired_at=4., layout_epoch=1)
        data = bytes([0,0,255,255, 255,0,0,255])
        if mode.startswith("metadata:"): result.update(json.loads(mode[9:]))
        if mode == "missing-field": del result["source_time"]
        if mode == "sequence-regress": result["frame_sequence"] = 2 if count == 1 else 1
        if mode in ("layout-regress", "layout-change") and count > 1:
            result["content_rect"] = [0,0,1,1]
            if mode == "layout-change": result["layout_epoch"] = 2
        if mode == "exit": sys.exit(3)
        if mode == "oversized-header":
            sys.stdout.buffer.write(b"x" * 65537); sys.stdout.buffer.flush(); continue
    h = dict(version=2, id=q["id"], ok=True, frame_bytes=len(data), result=result)
    if q["op"] == "capture":
        if mode == "wrong-id": h["id"] += 1
        if mode == "wrong-version": h["version"] = 1
        if mode == "bool-size": h["frame_bytes"] = True
        if mode == "oversized-payload": h["frame_bytes"] = 128*1024*1024+1
        if mode == "error": h.update(ok=False, frame_bytes=0, error=dict(code="target_closed", message="Target closed")); data=b""
        if mode == "changing-pixels" and count > 1: data = b"\x00"*8
    if data: shared.buf[:len(data)] = data
    encoded = json.dumps(h).encode()+b"\n"
    if mode == "truncated" and q["op"] == "capture":
        sys.stdout.buffer.write(encoded[:10]); sys.stdout.buffer.flush(); sys.exit()
    sys.stdout.buffer.write(encoded); sys.stdout.buffer.flush()
if shared is not None: shared.close()
"""


def command(mode="normal"):
    return [sys.executable, "-u", "-c", PEER, mode]


def session(mode="normal", **kwargs):
    return NativeCaptureSession(
        {"kind": "window", "window_id": 17, "pid": 29}, command=command(mode), **kwargs
    )


PROBE_PEER = r"""
import json, pathlib, sys, time
request = json.loads(sys.stdin.buffer.readline())
assert request == dict(version=0, id=1, op="close",
    session_id="00000000-0000-0000-0000-000000000001")
mode = sys.argv[1]
if mode == "cancel": pathlib.Path(sys.argv[2]).touch()
if mode in ("hang", "cancel"): time.sleep(30)
if mode in ("noisy-output", "noisy-errors"):
    stream = sys.stdout if mode == "noisy-output" else sys.stderr
    stream.write("x" * 65537)
    stream.flush()
    time.sleep(30)
if mode == "exit":
    sys.stderr.write("component loader failed")
    sys.exit(3)
if mode == "oversized":
    print("x" * 65537)
    sys.exit()
if mode == "malformed":
    print("not JSON")
    sys.exit()
response = dict(version=2, id=1 if mode == "unsupported_version" else 0, ok=False, frame_bytes=0,
    error=dict(code=mode if mode == "unsupported_version" else "protocol_version"))
if mode == "wrong-version": response["version"] = 1
if mode == "float-version": response["version"] = 2.0
if mode == "wrong-id": response["id"] = 2
if mode == "bool-id": response["id"] = False
if mode == "missing-id": del response["id"]
if mode == "success": response["ok"] = True
if mode == "bool-size": response["frame_bytes"] = False
if mode == "payload": response["frame_bytes"] = 4
if mode == "wrong-error": response["error"]["code"] = "invalid_session"
if mode == "malformed-error": response["error"]["code"] = []
if mode == "missing-error": del response["error"]
if mode == "array": response = []
print(json.dumps(response))
"""


class CaptureComponentProbeTest(unittest.TestCase):
    def setUp(self):
        self.command = self.enterContext(patch("vntts.native_capture.worker_command"))
        self.shared_memory = self.enterContext(
            patch("vntts.native_capture.SharedMemory")
        )
        self.children = []
        self.outputs = []
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            self.children.append(process)
            self.outputs.extend((kwargs["stdout"], kwargs["stderr"]))
            return process

        self.enterContext(patch("vntts.native_capture.Popen", side_effect=popen))

    def tearDown(self):
        self.shared_memory.assert_not_called()
        for process in self.children:
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stdin.closed)
        for output in self.outputs:
            self.assertTrue(output.closed)

    def peer(self, mode, *args):
        self.command.return_value = [sys.executable, "-c", PROBE_PEER, mode, *args]

    def test_launch_and_protocol_check_never_opens_a_capture_session(self):
        for rejection in ("protocol_version", "unsupported_version"):
            with self.subTest(rejection=rejection):
                self.peer(rejection)
                self.assertEqual(
                    probe_capture_worker(), " ".join(self.command.return_value)
                )

    def test_incompatible_and_unbounded_responses_are_rejected(self):
        for mode in (
            "wrong-version",
            "float-version",
            "wrong-id",
            "bool-id",
            "missing-id",
            "success",
            "bool-size",
            "payload",
            "wrong-error",
            "malformed-error",
            "missing-error",
            "array",
            "malformed",
            "oversized",
        ):
            with self.subTest(mode=mode):
                self.peer(mode)
                with self.assertRaises(CaptureError) as caught:
                    probe_capture_worker()
                self.assertEqual(caught.exception.code, "protocol_error")

    def test_noisy_live_worker_is_rejected_before_the_startup_deadline(self):
        for mode in ("noisy-output", "noisy-errors"):
            with self.subTest(mode=mode):
                self.peer(mode)
                with self.assertRaises(CaptureError) as caught:
                    probe_capture_worker(timeout=5)
                self.assertEqual(caught.exception.code, "protocol_error")

    def test_failed_launch_or_loader_error_is_reported(self):
        self.command.return_value = [
            str(Path(tempfile.gettempdir()) / "missing-capture-component")
        ]
        with self.assertRaises(OSError):
            probe_capture_worker()
        self.peer("exit")
        with self.assertRaisesRegex(CaptureError, "component loader failed"):
            probe_capture_worker()

    def test_hung_worker_is_killed_and_reaped_on_timeout_or_cancellation(self):
        self.peer("hang")
        with self.assertRaises(CaptureError) as caught:
            probe_capture_worker(timeout=0.1)
        self.assertEqual(caught.exception.code, "worker_timeout")
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "started"
            self.peer("cancel", str(marker))
            with self.assertRaises(RuntimePreparationCancelled):
                probe_capture_worker(cancellation=marker.is_file, timeout=5)

    def test_precancelled_probe_never_resolves_or_launches_worker(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(RuntimePreparationCancelled):
            probe_capture_worker(cancellation=cancelled)
        self.command.assert_not_called()
        self.assertEqual(self.children, [])


class NativeCaptureTest(unittest.TestCase):
    def assert_stopped(self, owner):
        self.assertIsNotNone(owner.process.poll())
        self.assertFalse(owner.reader.is_alive())
        self.assertTrue(owner.process.stdout.closed)
        self.assertTrue(owner.process.stdin.closed)
        self.assertIsNone(owner._frame_buffer)

    def test_next_frame_cannot_overwrite_an_owned_frame_or_another_session(self):
        with session("changing-pixels") as first, session() as second:
            first_name = first._frame_buffer.name
            second_name = second._frame_buffer.name
            self.assertNotEqual(first_name, second_name)
            original = first.capture()
            changed = first.capture()
            independent = second.capture()
            self.assertEqual(changed.pixels, b"\x00" * 8)
            self.assertEqual(original.pixels, independent.pixels)
            self.assertNotEqual(original.pixels, changed.pixels)
        self.assertNotEqual(original.pixels, changed.pixels)
        for name in (first_name, second_name):
            with self.assertRaises(FileNotFoundError):
                SharedMemory(name=name, track=False)

    def test_initialization_failure_releases_shared_buffer_and_started_process(self):
        real_shared = SharedMemory
        allocations = []
        children = []
        real_popen = subprocess.Popen

        def allocate(*args, **kwargs):
            block = real_shared(*args, **kwargs)
            allocations.append(block.name)
            return block

        def popen(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        for stage in ("spawn", "reader", "open"):
            with (
                self.subTest(stage=stage),
                patch("vntts.native_capture.SharedMemory", side_effect=allocate),
                patch(
                    "vntts.native_capture.Popen",
                    side_effect=OSError("spawn failed") if stage == "spawn" else popen,
                ),
                patch(
                    "vntts.native_capture.Thread.start",
                    side_effect=RuntimeError("thread failed"),
                )
                if stage == "reader"
                else nullcontext(),
            ):
                with self.assertRaises((OSError, RuntimeError)):
                    session("wrong-session-open" if stage == "open" else "normal")
            with self.assertRaises(FileNotFoundError):
                real_shared(name=allocations[-1], track=False)
        for child in children:
            self.assertIsNotNone(child.poll())
            self.assertTrue(child.stdout.closed)

    def test_session_identity_is_a_canonical_uuid_on_the_wire(self):
        with session() as owner:
            identity = UUID(owner.session_id)
            self.assertEqual(str(identity), owner.session_id)
            self.assertEqual(identity.version, 4)
            self.assertEqual(owner.capture().session_id, str(identity))

    def test_owned_pixels_and_explicit_clock_survive_close_and_unchanged_frame(self):
        with session() as owner:
            first = owner.capture()
            second = owner.capture()
            self.assertEqual(first, second)
            self.assertEqual(owner.worker_clock_at_open, 500.0)
            self.assertEqual(first.source_time, 1.0)
            self.assertEqual(first.acquired_at, 4.0)
            self.assertEqual(first.desktop_bounds, (100, 200, 200, 100))
            self.assertEqual(
                list(first.to_image().get_flattened_data()), [(255, 0, 0), (0, 0, 255)]
            )
            with self.assertRaises(FrozenInstanceError):
                first.width = 4
        self.assert_stopped(owner)
        self.assertEqual(first.pixels, bytes([0, 0, 255, 255, 255, 0, 0, 255]))
        owner.close()

    def test_corrupt_framing_reaps_worker_and_reader(self):
        for mode in (
            "wrong-id",
            "wrong-version",
            "bool-size",
            "oversized-header",
            "oversized-payload",
            "truncated",
            "exit",
        ):
            with self.subTest(mode=mode):
                owner = session(mode)
                with self.assertRaises(CaptureError):
                    owner.capture()
                self.assert_stopped(owner)

    def test_invalid_metadata_never_returns_other_target_or_inconsistent_pixels(self):
        for changes in (
            {"session_id": "other"},
            {"window_id": 18},
            {"pid": 30},
            {"pid": True},
            {"scope": "display"},
            {"width": True},
            {"width": 0},
            {"width": 33 * 1024 * 1024},
            {"row_stride": 9},
            {"content_rect": [1, 0, 2, 1]},
            {"content_rect": [0, 0, 0, 1]},
            {"desktop_bounds": [0, 0, -1, 1]},
            {"pixel_format": "RGBA8"},
            {"frame_sequence": 0},
            {"layout_epoch": 0},
            {"acquired_at": None},
            {"source_time": "1"},
            {"acquired_at": float("inf")},
        ):
            with self.subTest(changes=changes):
                owner = session("metadata:" + json.dumps(changes))
                with self.assertRaises(CaptureError):
                    owner.capture()
                self.assert_stopped(owner)

    def test_layout_changes_require_a_new_epoch_and_sequence_never_regresses(self):
        for mode in ("sequence-regress", "layout-regress"):
            with self.subTest(mode=mode):
                owner = session(mode)
                owner.capture()
                with self.assertRaises(CaptureError):
                    owner.capture()
                self.assert_stopped(owner)
        with session("layout-change") as owner:
            owner.capture()
            resized = owner.capture()
            self.assertEqual(resized.content_rect, (0, 0, 1, 1))
            self.assertEqual(resized.layout_epoch, 2)

    def test_missing_metadata_is_a_protocol_failure(self):
        owner = session("missing-field")
        with self.assertRaisesRegex(CaptureError, "incomplete"):
            owner.capture()
        self.assert_stopped(owner)

    def test_worker_failure_preserves_code(self):
        owner = session("error")
        with self.assertRaises(CaptureError) as raised:
            owner.capture()
        self.assertEqual(raised.exception.code, "target_closed")
        self.assert_stopped(owner)

    def test_timeout_discards_worker_instead_of_reusing_late_response(self):
        owner = session("hang")
        with self.assertRaises(CaptureError) as raised:
            owner.capture(timeout_ms=1)
        self.assertEqual(raised.exception.code, "timeout")
        self.assert_stopped(owner)
        with self.assertRaises(CaptureError):
            owner.capture()

    def test_cancel_interrupts_pending_read_and_reaps(self):
        owner = session("hang")
        cancel = threading.Event()
        timer = threading.Timer(0.05, cancel.set)
        timer.start()
        try:
            with self.assertRaises(CaptureError) as raised:
                owner.capture(cancel=cancel)
            self.assertEqual(raised.exception.code, "cancelled")
        finally:
            timer.join()
            owner.close()
        self.assert_stopped(owner)

    def test_close_interrupts_pending_capture(self):
        owner = session("hang")
        errors = []

        def capture():
            try:
                owner.capture()
            except CaptureError as error:
                errors.append(error)

        pending = threading.Thread(target=capture)
        pending.start()
        owner.close()
        pending.join(1)
        self.assertFalse(pending.is_alive())
        self.assertEqual(len(errors), 1)
        self.assert_stopped(owner)

    def test_failed_open_reaps_started_process(self):
        real_popen = subprocess.Popen
        children = []

        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            return process

        for mode in ("hang-open", "wrong-session-open"):
            with (
                self.subTest(mode=mode),
                patch("vntts.native_capture.Popen", side_effect=popen),
            ):
                with self.assertRaises(CaptureError):
                    session(mode, startup_timeout=0.05)
                self.assertIsNotNone(children[-1].poll())
                self.assertTrue(children[-1].stdout.closed)

    def test_invalid_target_never_starts_process(self):
        with patch("vntts.native_capture.Popen") as popen:
            for target in (
                {"kind": "window", "window_id": 1},
                {"kind": "window", "window_id": True, "pid": 2},
                {"kind": "display", "display_id": -1},
            ):
                with self.assertRaises(ValueError):
                    NativeCaptureSession(target)
            with self.assertRaises(ValueError):
                NativeCaptureSession({"kind": "display", "display_id": 0}, max_fps=61)
            popen.assert_not_called()


class WorkerResolverTest(unittest.TestCase):
    def test_component_probe_bypasses_wayland_preflight_but_capture_still_refuses_it(
        self,
    ):
        from vntts.window_capture import WindowCaptureUnavailableError

        real_popen = subprocess.Popen
        expected = [sys.executable, "-m", "vntts.x11_capture_worker"]

        def peer(command, **kwargs):
            self.assertEqual(command, expected)
            return real_popen(
                [sys.executable, "-c", PROBE_PEER, "protocol_version"], **kwargs
            )

        with (
            patch("vntts.native_capture.sys.platform", "linux"),
            patch("vntts.native_capture.sys.frozen", False, create=True),
            patch.dict(
                os.environ,
                {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"},
                clear=True,
            ),
            patch("vntts.native_capture.Popen", side_effect=peer) as launch,
        ):
            with self.assertRaisesRegex(
                WindowCaptureUnavailableError, "native Wayland"
            ):
                worker_command()
            launch.assert_not_called()
            self.assertEqual(probe_capture_worker(), " ".join(expected))
            launch.assert_called_once()

    def test_explicit_worker_requires_executable_without_starting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker"
            with patch.dict(os.environ, {"VNTTS_CAPTURE_WORKER": str(path)}):
                with self.assertRaisesRegex(CaptureError, "VNTTS_CAPTURE_WORKER"):
                    worker_executable()
                path.write_text("fixture")
                path.chmod(0o700)
                self.assertEqual(worker_executable(), path)

    def test_source_paths_and_build_guidance(self):
        for platform, suffix in (
            (
                "darwin",
                "macos/build/vntts-capture-worker",
            ),
            (
                "win32",
                "windows/target/release/vntts-capture-worker.exe",
            ),
        ):
            with (
                self.subTest(platform=platform),
                patch.dict(os.environ, {}, clear=True),
                patch("vntts.native_capture.sys.platform", platform),
                patch("vntts.native_capture.Path.is_file", return_value=False),
            ):
                with self.assertRaisesRegex(
                    CaptureError, "uv run --no-project python scripts/build.py"
                ):
                    worker_executable()
                with (
                    patch("vntts.native_capture.Path.is_file", return_value=True),
                    patch("vntts.native_capture.os.access", return_value=True),
                ):
                    self.assertTrue(worker_executable().as_posix().endswith(suffix))

    def test_frozen_bundle_path(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("vntts.native_capture.sys.platform", "win32"),
            patch("vntts.native_capture.sys.frozen", True, create=True),
            patch("vntts.native_capture.sys._MEIPASS", "bundle", create=True),
            patch("vntts.native_capture.Path.is_file", return_value=True),
        ):
            self.assertEqual(
                worker_executable(),
                Path("bundle/native-capture/vntts-capture-worker.exe"),
            )

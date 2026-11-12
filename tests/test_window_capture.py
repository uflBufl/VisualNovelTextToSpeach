import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psutil

from vntts.window_capture import (
    LinuxX11WindowBackend,
    MacOSWindowBackend,
    WaylandWindowBackend,
    WindowCaptureTarget,
    WindowCaptureUnavailableError,
    WindowGeometry,
    WindowInfo,
    WindowMinimizedError,
    WindowNotFoundError,
    create_window_backend,
    ensure_screen_capture_supported,
    is_native_wayland_session,
)


class FakeQuartz:
    kCGWindowLayer = "layer"
    kCGWindowBounds = "bounds"
    kCGWindowOwnerName = "owner"
    kCGWindowName = "name"
    kCGWindowNumber = "number"
    kCGWindowOwnerPID = "pid"
    kCGWindowIsOnscreen = "onscreen"
    kCGWindowListOptionAll = 0
    kCGWindowListOptionOnScreenOnly = 1
    kCGWindowListExcludeDesktopElements = 2
    kCGWindowListOptionIncludingWindow = 4
    kCGNullWindowID = 0

    def __init__(self, windows):
        self.windows = windows

    def CGWindowListCopyWindowInfo(self, option, window_id):
        if option == self.kCGWindowListOptionIncludingWindow:
            return [
                window
                for window in self.windows
                if window[self.kCGWindowNumber] == window_id
            ]
        if option & self.kCGWindowListOptionOnScreenOnly:
            return [window for window in self.windows if window["onscreen"]]
        return self.windows

    def CGGetActiveDisplayList(self, _maximum, _displays, _count):
        return 0, (1,), 1

    def CGDisplayBounds(self, _display_id):
        return SimpleNamespace(
            origin=SimpleNamespace(x=0, y=0),
            size=SimpleNamespace(width=1920, height=1080),
        )


class FakeX11Window:
    def __init__(self, handle, properties=None, *, geometry=None, map_state=2):
        self.id = handle
        self.properties = properties or {}
        self.geometry = geometry or SimpleNamespace(width=1600, height=900)
        self.map_state = map_state
        self.translations = []

    def get_full_property(self, atom, _property_type):
        value = self.properties.get(atom)
        return None if value is None else SimpleNamespace(value=value)

    def get_wm_name(self):
        return self.properties.get("WM_NAME")

    def get_attributes(self):
        return SimpleNamespace(map_state=self.map_state)

    def get_geometry(self):
        return self.geometry

    def translate_coords(self, source, x, y):
        self.translations.append((source.id, x, y))
        return SimpleNamespace(
            x=120 if self.id == 1 else -120, y=80 if self.id == 1 else -80
        )

    def query_tree(self):
        return SimpleNamespace(children=[])


class FakeX11Display:
    def __init__(self, windows, client_ids):
        self.windows = {window.id: window for window in windows}
        self.root = FakeX11Window(
            1,
            {"_NET_CLIENT_LIST_STACKING": client_ids},
        )

    def screen(self):
        return SimpleNamespace(root=self.root)

    def intern_atom(self, name):
        return name

    def create_resource_object(self, resource_type, handle):
        if resource_type != "window" or handle not in self.windows:
            raise KeyError(handle)
        return self.windows[handle]


class WindowCaptureTargetTest(unittest.TestCase):
    def test_geometry_follows_window_movement_and_resize_for_keyboard_dispatch(self):
        window = WindowInfo(100, "Reverse: 1999", 42)
        backend = Mock()
        backend.list_windows.return_value = [window]
        backend.get_window.return_value = window
        layouts = [
            WindowGeometry(100, 200, 1600, 900),
            WindowGeometry(300, 100, 1920, 1080),
        ]
        backend.get_client_geometry.side_effect = layouts
        target = WindowCaptureTarget("Reverse: 1999", backend)
        self.assertEqual(target.get_geometry(), layouts[0])
        self.assertEqual(target.get_geometry(), layouts[1])

    def test_destroyed_window_cannot_silently_bind_restarted_process(self):
        old_window = WindowInfo(100, "Reverse: 1999", 42)
        new_window = WindowInfo(200, "Reverse: 1999", 84)
        backend = Mock()
        backend.list_windows.side_effect = [[old_window], [new_window]]
        backend.get_window.return_value = None
        backend.get_client_geometry.return_value = WindowGeometry(0, 0, 1920, 1080)
        target = WindowCaptureTarget("Reverse: 1999", backend)
        self.assertEqual(target.get_geometry().width, 1920)
        with self.assertRaisesRegex(WindowNotFoundError, "select it again"):
            target.get_geometry()
        backend.get_client_geometry.assert_called_once_with(100)

    def test_minimized_window_reports_recoverable_error(self):
        backend = Mock()
        backend.list_windows.return_value = [
            WindowInfo(100, "Reverse: 1999", 42, minimized=True)
        ]
        target = WindowCaptureTarget("Reverse: 1999", backend)

        with self.assertRaisesRegex(WindowMinimizedError, "is minimized"):
            target.get_geometry()

        backend.get_client_geometry.assert_not_called()

    def test_missing_window_reports_selected_title(self):
        backend = Mock()
        backend.list_windows.return_value = []
        target = WindowCaptureTarget("Reverse: 1999", backend)

        with self.assertRaisesRegex(WindowNotFoundError, "Reverse: 1999"):
            target.get_geometry()

    def test_empty_selection_requests_configuration(self):
        target = WindowCaptureTarget("", Mock())

        with self.assertRaisesRegex(WindowNotFoundError, "Select a game window"):
            target.get_geometry()

    def test_reports_whether_selected_window_is_foreground(self):
        window = WindowInfo(100, "Reverse: 1999", 42)
        backend = Mock()
        backend.list_windows.return_value = [window]
        backend.get_window.return_value = window
        backend.get_foreground_handle.side_effect = [100, 200]
        target = WindowCaptureTarget("Reverse: 1999", backend)

        self.assertTrue(target.is_focused())
        self.assertFalse(target.is_focused())

    def test_input_focus_snapshot_reports_ids_without_changing_selection(self):
        selected = WindowInfo(100, "Reverse: 1999 PRIVATE title", 42)
        other = WindowInfo(200, "PRIVATE other title", 84)
        backend = Mock()
        backend.list_windows.return_value = [selected]
        backend.get_window.side_effect = lambda handle: {100: selected, 200: other}.get(
            handle
        )
        backend.get_foreground_handle.return_value = 200
        target = WindowCaptureTarget("Reverse: 1999", backend)

        self.assertEqual(
            target.input_focus_snapshot(),
            {
                "selected_window_id": 100,
                "selected_pid": 42,
                "foreground_window_id": 200,
                "foreground_pid": 84,
                "focused": False,
            },
        )
        self.assertIsNone(target._handle)
        self.assertIsNone(target.last_window)
        target.get_geometry()
        backend.get_foreground_handle.return_value = 100
        self.assertTrue(target.input_focus_snapshot()["focused"])
        self.assertEqual(target._handle, 100)
        self.assertIs(target.last_window, selected)

    def test_input_focus_snapshot_omits_unknown_foreground_and_propagates_failure(self):
        backend = Mock()
        backend.list_windows.return_value = [WindowInfo(100, "Game", 42)]
        backend.get_foreground_handle.return_value = 0
        target = WindowCaptureTarget("Game", backend)
        self.assertEqual(
            target.input_focus_snapshot(),
            {
                "selected_window_id": 100,
                "selected_pid": 42,
                "focused": False,
            },
        )
        backend.get_foreground_handle.side_effect = OSError("PRIVATE failure")
        with self.assertRaises(OSError):
            target.input_focus_snapshot()
        self.assertIsNone(target.last_window)

    def test_input_focus_snapshot_does_not_retarget_another_process(self):
        selected = WindowInfo(100, "Game", 42)
        backend = Mock()
        backend.list_windows.return_value = [selected]
        backend.get_window.return_value = selected
        target = WindowCaptureTarget("Game", backend)
        target.get_geometry()
        backend.get_window.return_value = None
        backend.list_windows.return_value = [WindowInfo(200, "Game", 84)]

        with self.assertRaises(WindowNotFoundError):
            target.input_focus_snapshot()
        self.assertEqual(target._handle, 100)
        self.assertIs(target.last_window, selected)


class WindowCaptureIdentityTest(unittest.TestCase):
    def setUp(self):
        self.window = WindowInfo(100, "Game", 42)
        self.backend = Mock()
        self.backend.list_windows.return_value = [self.window]
        self.backend.get_window.return_value = self.window
        self.target = WindowCaptureTarget("Game", self.backend)
        self.addCleanup(self.target.close)
        self.worker = Mock(clock_offset=0.0, clock_uncertainty=0.0)
        factory = patch(
            "vntts.window_capture.NativeCaptureSession", return_value=self.worker
        )
        self.create = factory.start()
        self.addCleanup(factory.stop)
        process = patch("vntts.window_capture.psutil.Process")
        self.process = process.start()
        self.addCleanup(process.stop)
        fingerprint = patch(
            "vntts.window_capture.process_fingerprint",
            return_value={"scheme": "test", "token": "original-process"},
        )
        self.fingerprint = fingerprint.start()
        self.addCleanup(fingerprint.stop)

    def test_wall_clock_adjustment_keeps_selected_process_across_source_reopen(self):
        self.process.return_value.create_time.return_value = 1000.0
        self.target.capture_native()
        self.process.return_value.create_time.return_value = 999.0
        self.target.capture_native()
        self.create.assert_called_once()
        self.target.close()
        self.target.capture_native()
        self.assertEqual(self.create.call_count, 2)
        self.assertEqual(self.fingerprint.call_count, 3)

    def test_pid_reuse_is_rejected_after_source_close_even_for_same_wall_clock_start(
        self,
    ):
        self.process.return_value.create_time.return_value = 1000.0
        self.target.capture_native()
        self.target.close()
        self.fingerprint.return_value = {"scheme": "test", "token": "new-process"}
        with self.assertRaisesRegex(WindowNotFoundError, "process changed"):
            self.target.capture_native()
        self.create.assert_called_once()

    def test_unavailable_process_discards_source_and_keeps_selected_owner(self):
        for error in (psutil.NoSuchProcess(42), psutil.AccessDenied(42)):
            with self.subTest(error=type(error).__name__):
                self.target.capture_native()
                self.fingerprint.side_effect = error
                with self.assertRaisesRegex(
                    WindowNotFoundError, "unavailable"
                ) as caught:
                    self.target.capture_native()
                self.assertIs(caught.exception.__cause__, error)
                self.assertIsNone(self.target._source)
                self.fingerprint.side_effect = None
                self.target.capture_native()

    def test_minimized_window_does_not_probe_process_or_open_source(self):
        self.backend.list_windows.return_value = [
            WindowInfo(100, "Game", 42, minimized=True)
        ]
        with self.assertRaises(WindowMinimizedError):
            self.target.capture_native()
        self.process.assert_not_called()
        self.fingerprint.assert_not_called()
        self.create.assert_not_called()


class MacOSWindowBackendTest(unittest.TestCase):
    def window(self, number, owner, *, name="", onscreen=True, layer=0):
        return {
            "number": number,
            "owner": owner,
            "name": name,
            "pid": os.getpid() + number,
            "layer": layer,
            "onscreen": onscreen,
            "bounds": {"X": 120, "Y": 80, "Width": 1600, "Height": 900},
        }

    def test_lists_named_application_windows_and_ignores_overlays(self):
        quartz = FakeQuartz(
            [
                self.window(10, "Reverse: 1999", name="Game"),
                self.window(11, "Dock", layer=20),
                self.window(12, "Desktop", onscreen=False),
            ]
        )

        windows = MacOSWindowBackend(quartz).list_windows()

        self.assertEqual(
            windows,
            [WindowInfo(10, "Reverse: 1999 - Game", os.getpid() + 10)],
        )

    def test_returns_quartz_window_geometry(self):
        quartz = FakeQuartz([self.window(10, "Reverse: 1999")])
        backend = MacOSWindowBackend(quartz)

        self.assertEqual(
            backend.get_client_geometry(10),
            WindowGeometry(120, 80, 1600, 900),
        )

    def test_exact_lookup_survives_fullscreen_space_changes_and_rejects_missing_target(
        self,
    ):
        other = self.window(20, "Game")
        selected = self.window(10, "Game")
        selected["bounds"] = {"X": 0, "Y": 0, "Width": 1728, "Height": 1080}
        quartz = FakeQuartz([other, selected])
        quartz.CGWindowListCopyWindowInfo = Mock(
            side_effect=lambda option, window_id: (
                quartz.windows
                if option == quartz.kCGWindowListOptionAll
                and window_id == quartz.kCGNullWindowID
                else []
            )
        )
        backend = MacOSWindowBackend(quartz)

        self.assertEqual(
            backend.get_window(10), WindowInfo(10, "Game", selected["pid"])
        )
        self.assertEqual(
            backend.get_client_geometry(10), WindowGeometry(0, 0, 1728, 1080)
        )
        quartz.windows = [other]
        self.assertIsNone(backend.get_window(10))
        with self.assertRaises(WindowNotFoundError):
            backend.get_client_geometry(10)

    def test_accepts_fullscreen_window_coordinates_outside_primary_display(self):
        window = self.window(10, "Reverse: 1999")
        window["bounds"] = {
            "X": 1919,
            "Y": 1079,
            "Width": 1600,
            "Height": 900,
        }
        backend = MacOSWindowBackend(FakeQuartz([window]))

        self.assertEqual(
            backend.get_client_geometry(10),
            WindowGeometry(1919, 1079, 1600, 900),
        )

    def test_frontmost_quartz_window_is_used_for_focus(self):
        quartz = FakeQuartz(
            [
                self.window(20, "Front game"),
                self.window(10, "Background game"),
            ]
        )

        self.assertEqual(MacOSWindowBackend(quartz).get_foreground_handle(), 20)

    def test_frontmost_application_pid_is_the_keyboard_focus_authority(self):
        application = SimpleNamespace(processIdentifier=lambda: 84)
        workspace = SimpleNamespace(frontmostApplication=lambda: application)
        backend = MacOSWindowBackend(
            FakeQuartz(
                [
                    self.window(20, "Fullscreen game"),
                    self.window(10, "Actually focused app"),
                ]
            ),
            workspace=workspace,
        )

        self.assertEqual(backend.get_foreground_process_id(), 84)

    def test_target_compares_macos_active_process_not_quartz_z_order(self):
        game = self.window(20, "Reverse: 1999")
        game["pid"] = 84
        application = SimpleNamespace(processIdentifier=lambda: 91)
        workspace = SimpleNamespace(frontmostApplication=lambda: application)
        target = WindowCaptureTarget(
            "Reverse: 1999",
            MacOSWindowBackend(FakeQuartz([game]), workspace=workspace),
        )

        self.assertFalse(target.is_focused())

        application.processIdentifier = lambda: 84
        self.assertTrue(target.is_focused())

    def test_input_focus_snapshot_uses_macos_frontmost_pid_not_z_order(self):
        game = self.window(20, "Game")
        game["pid"] = 84
        application = SimpleNamespace(processIdentifier=lambda: 91)
        target = WindowCaptureTarget(
            "Game",
            MacOSWindowBackend(
                FakeQuartz([game]),
                workspace=SimpleNamespace(frontmostApplication=lambda: application),
            ),
        )
        self.assertEqual(
            target.input_focus_snapshot(),
            {
                "selected_window_id": 20,
                "selected_pid": 84,
                "foreground_pid": 91,
                "focused": False,
            },
        )
        application.processIdentifier = lambda: 84
        self.assertTrue(target.input_focus_snapshot()["focused"])
        application.processIdentifier = lambda: 0
        self.assertEqual(
            target.input_focus_snapshot(),
            {
                "selected_window_id": 20,
                "selected_pid": 84,
                "focused": False,
            },
        )

    def test_reports_window_that_disappeared(self):
        backend = MacOSWindowBackend(FakeQuartz([]))

        with self.assertRaisesRegex(WindowNotFoundError, "no longer available"):
            backend.get_client_geometry(10)

    def test_platform_factory_selects_macos_backend(self):
        backend = Mock()
        with patch(
            "vntts.window_capture.MacOSWindowBackend",
            return_value=backend,
        ) as backend_factory:
            selected = create_window_backend(platform="darwin")

        self.assertIs(selected, backend)
        backend_factory.assert_called_once_with()


class LinuxWindowBackendTest(unittest.TestCase):
    def test_x11_lists_windows_and_returns_root_relative_geometry(self):
        process_id = os.getpid() + 100
        game = FakeX11Window(
            100,
            {
                "_NET_WM_NAME": b"Reverse: 1999",
                "_NET_WM_PID": [process_id],
            },
        )
        display = FakeX11Display([game], [100])
        backend = LinuxX11WindowBackend(display=display)

        self.assertEqual(
            backend.list_windows(),
            [WindowInfo(100, "Reverse: 1999", process_id)],
        )
        self.assertEqual(
            backend.get_client_geometry(100),
            WindowGeometry(120, 80, 1600, 900),
        )
        self.assertEqual(display.root.translations, [(game.id, 0, 0)])
        self.assertEqual(game.translations, [])

    def test_x11_empty_pid_property_defaults_to_zero(self):
        game = FakeX11Window(
            100,
            {
                "_NET_WM_NAME": b"Reverse: 1999",
                "_NET_WM_PID": [],
            },
        )

        self.assertEqual(
            LinuxX11WindowBackend(display=FakeX11Display([game], [100])).list_windows(),
            [WindowInfo(100, "Reverse: 1999", 0)],
        )

    def test_x11_reports_hidden_window_as_minimized(self):
        game = FakeX11Window(
            100,
            {
                "_NET_WM_NAME": b"Reverse: 1999",
                "_NET_WM_PID": [os.getpid() + 100],
                "_NET_WM_STATE": ["_NET_WM_STATE_HIDDEN"],
            },
        )
        backend = LinuxX11WindowBackend(display=FakeX11Display([game], [100]))

        self.assertTrue(backend.get_window(100).minimized)

    def test_x11_reads_active_window_for_focus(self):
        display = FakeX11Display([], [])
        display.root.properties["_NET_ACTIVE_WINDOW"] = [100]

        self.assertEqual(
            LinuxX11WindowBackend(display=display).get_foreground_handle(),
            100,
        )

    def test_factory_selects_x11_for_linux_x11_session(self):
        backend = Mock()
        environment = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
        with patch(
            "vntts.window_capture.LinuxX11WindowBackend",
            return_value=backend,
        ) as backend_factory:
            selected = create_window_backend(
                platform="linux",
                environment=environment,
            )

        self.assertIs(selected, backend)
        backend_factory.assert_called_once_with(environment=environment)

    def test_native_wayland_is_explicitly_refused(self):
        environment = {
            "XDG_SESSION_TYPE": "wayland",
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0",
        }

        self.assertTrue(
            is_native_wayland_session(
                platform="linux",
                environment=environment,
            )
        )
        self.assertIsInstance(
            create_window_backend(
                platform="linux",
                environment=environment,
            ),
            WaylandWindowBackend,
        )
        with self.assertRaisesRegex(
            WindowCaptureUnavailableError,
            "native Wayland",
        ):
            ensure_screen_capture_supported(
                platform="linux",
                environment=environment,
            )

    def test_xwayland_variable_does_not_override_explicit_x11_session(self):
        self.assertFalse(
            is_native_wayland_session(
                platform="linux",
                environment={
                    "XDG_SESSION_TYPE": "x11",
                    "WAYLAND_DISPLAY": "wayland-0",
                    "DISPLAY": ":0",
                },
            )
        )


if __name__ == "__main__":
    unittest.main()

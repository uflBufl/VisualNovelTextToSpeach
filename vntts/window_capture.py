import ctypes
import os
import sys
from dataclasses import dataclass


class WindowCaptureError(RuntimeError):
    pass


class WindowCaptureUnavailableError(WindowCaptureError):
    pass


class WindowNotFoundError(WindowCaptureError):
    pass


class WindowMinimizedError(WindowCaptureError):
    pass


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    process_id: int
    minimized: bool = False


@dataclass(frozen=True)
class WindowGeometry:
    left: int
    top: int
    width: int
    height: int

    def as_monitor(self):
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


class Win32WindowBackend:
    def __init__(self, user32=None):
        if sys.platform != "win32" and user32 is None:
            raise WindowCaptureUnavailableError(
                "Game-window capture is available only on Windows"
            )

        from ctypes import wintypes

        self.wintypes = wintypes
        self.user32 = user32 or ctypes.WinDLL("user32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )
        self._configure_functions()

    def _configure_functions(self):
        wintypes = self.wintypes
        functions = {
            "EnumWindows": ([self.callback_type, wintypes.LPARAM], wintypes.BOOL),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
            "GetWindowTextW": (
                [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int],
                ctypes.c_int,
            ),
            "GetWindowThreadProcessId": (
                [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
                wintypes.DWORD,
            ),
            "GetClientRect": (
                [wintypes.HWND, ctypes.POINTER(wintypes.RECT)],
                wintypes.BOOL,
            ),
            "ClientToScreen": (
                [wintypes.HWND, ctypes.POINTER(wintypes.POINT)],
                wintypes.BOOL,
            ),
            "GetForegroundWindow": ([], wintypes.HWND),
        }
        for name, (argument_types, result_type) in functions.items():
            function = getattr(self.user32, name)
            function.argtypes = argument_types
            function.restype = result_type

    def list_windows(self):
        windows = []

        @self.callback_type
        def collect(handle, _parameter):
            window = self.get_window(handle)
            if window is not None and window.process_id != os.getpid():
                windows.append(window)
            return True

        if not self.user32.EnumWindows(collect, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return sorted(windows, key=lambda window: window.title.casefold())

    def get_window(self, handle):
        if not self.user32.IsWindow(handle):
            return None
        if not self.user32.IsWindowVisible(handle):
            return None
        title_length = self.user32.GetWindowTextLengthW(handle)
        if title_length <= 0:
            return None
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        if self.user32.GetWindowTextW(handle, title_buffer, len(title_buffer)) <= 0:
            return None
        title = title_buffer.value.strip()
        if not title:
            return None
        process_id = self.wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        handle_value = handle.value if hasattr(handle, "value") else handle
        return WindowInfo(
            handle=int(handle_value),
            title=title,
            process_id=process_id.value,
            minimized=bool(self.user32.IsIconic(handle)),
        )

    def get_client_geometry(self, handle):
        rectangle = self.wintypes.RECT()
        if not self.user32.GetClientRect(handle, ctypes.byref(rectangle)):
            raise ctypes.WinError(ctypes.get_last_error())

        top_left = self.wintypes.POINT(rectangle.left, rectangle.top)
        bottom_right = self.wintypes.POINT(rectangle.right, rectangle.bottom)
        if not self.user32.ClientToScreen(handle, ctypes.byref(top_left)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.user32.ClientToScreen(handle, ctypes.byref(bottom_right)):
            raise ctypes.WinError(ctypes.get_last_error())

        width = bottom_right.x - top_left.x
        height = bottom_right.y - top_left.y
        if width <= 0 or height <= 0:
            raise WindowCaptureError("Selected game window has no visible client area")
        return WindowGeometry(top_left.x, top_left.y, width, height)

    def get_foreground_handle(self):
        handle = self.user32.GetForegroundWindow()
        return int(handle.value if hasattr(handle, "value") else handle or 0)


class MacOSWindowBackend:
    def __init__(self, quartz=None, workspace=None):
        if sys.platform != "darwin" and quartz is None:
            raise WindowCaptureUnavailableError(
                "macOS game-window capture is available only on macOS"
            )
        if quartz is None:
            try:
                import Quartz as quartz
            except ImportError as error:
                raise WindowCaptureUnavailableError(
                    "macOS window capture requires pyobjc-framework-Quartz"
                ) from error
        self.quartz = quartz
        self.workspace = workspace

    def _window_info(self, values):
        quartz = self.quartz
        layer = int(values.get(quartz.kCGWindowLayer, 0))
        bounds = values.get(quartz.kCGWindowBounds) or {}
        width = int(round(float(bounds.get("Width", 0))))
        height = int(round(float(bounds.get("Height", 0))))
        owner = str(values.get(quartz.kCGWindowOwnerName) or "").strip()
        name = str(values.get(quartz.kCGWindowName) or "").strip()
        if not owner or layer != 0 or width <= 0 or height <= 0:
            return None
        title = f"{owner} - {name}" if name and name != owner else owner
        return WindowInfo(
            handle=int(values[quartz.kCGWindowNumber]),
            title=title,
            process_id=int(values.get(quartz.kCGWindowOwnerPID, 0)),
            minimized=not bool(values.get(quartz.kCGWindowIsOnscreen, False)),
        )

    def _copy_windows(self, option, window_id=0):
        windows = self.quartz.CGWindowListCopyWindowInfo(option, window_id)
        if windows is None:
            raise WindowCaptureError(
                "macOS did not return window information; allow Screen Recording "
                "access in System Settings"
            )
        return windows

    def list_windows(self):
        quartz = self.quartz
        values = self._copy_windows(
            quartz.kCGWindowListOptionOnScreenOnly
            | quartz.kCGWindowListExcludeDesktopElements,
            quartz.kCGNullWindowID,
        )
        windows = [self._window_info(item) for item in values]
        windows = [
            window
            for window in windows
            if window is not None and window.process_id != os.getpid()
        ]
        return sorted(windows, key=lambda window: window.title.casefold())

    def get_window(self, handle):
        quartz = self.quartz
        values = self._copy_windows(
            quartz.kCGWindowListOptionIncludingWindow,
            int(handle),
        )
        for item in values:
            if int(item.get(quartz.kCGWindowNumber, -1)) == int(handle):
                return self._window_info(item)
        return None

    def get_client_geometry(self, handle):
        quartz = self.quartz
        values = self._copy_windows(
            quartz.kCGWindowListOptionIncludingWindow,
            int(handle),
        )
        for item in values:
            if int(item.get(quartz.kCGWindowNumber, -1)) != int(handle):
                continue
            bounds = item.get(quartz.kCGWindowBounds) or {}
            geometry = WindowGeometry(
                left=int(round(float(bounds.get("X", 0)))),
                top=int(round(float(bounds.get("Y", 0)))),
                width=int(round(float(bounds.get("Width", 0)))),
                height=int(round(float(bounds.get("Height", 0)))),
            )
            if geometry.width <= 0 or geometry.height <= 0:
                raise WindowCaptureError(
                    "Selected game window has no visible capture area"
                )
            return geometry
        raise WindowNotFoundError("Selected macOS game window is no longer available")

    def get_foreground_handle(self):
        quartz = self.quartz
        values = self._copy_windows(
            quartz.kCGWindowListOptionOnScreenOnly
            | quartz.kCGWindowListExcludeDesktopElements,
            quartz.kCGNullWindowID,
        )
        for item in values:
            window = self._window_info(item)
            if window is not None:
                return window.handle
        return 0

    def get_foreground_process_id(self):
        """Return the application that actually owns keyboard focus.

        Quartz window ordering is not an application-focus authority on macOS:
        a fullscreen window on another display or Space can remain first in the
        returned Z-order. NSWorkspace reports the process that receives an
        unaddressed keyboard event, which is the identity auto advance needs.
        """
        workspace = self.workspace
        if workspace is None:
            from AppKit import NSWorkspace

            workspace = NSWorkspace.sharedWorkspace()
            self.workspace = workspace
        application = workspace.frontmostApplication()
        if application is None:
            return 0
        return int(application.processIdentifier())


class LinuxX11WindowBackend:
    def __init__(self, display=None, environment=None):
        environment = os.environ if environment is None else environment
        if display is None and sys.platform != "linux":
            raise WindowCaptureUnavailableError(
                "X11 game-window capture is available only on Linux"
            )
        if display is None and not environment.get("DISPLAY"):
            raise WindowCaptureUnavailableError(
                "X11 capture requires an interactive session with DISPLAY set"
            )
        if display is None:
            try:
                from Xlib.display import Display
            except ImportError as error:
                raise WindowCaptureUnavailableError(
                    "X11 game-window capture requires python-xlib"
                ) from error
            try:
                display = Display()
            except Exception as error:
                raise WindowCaptureUnavailableError(
                    f"Unable to connect to the X11 display: {error}"
                ) from error
        self.display = display
        self.root = display.screen().root
        self._atoms = {}

    def _atom(self, name):
        if name not in self._atoms:
            self._atoms[name] = self.display.intern_atom(name)
        return self._atoms[name]

    def _property(self, window, name, property_type=0):
        try:
            value = window.get_full_property(self._atom(name), property_type)
        except Exception:
            return None
        return None if value is None else value.value

    def _client_ids(self):
        values = self._property(self.root, "_NET_CLIENT_LIST_STACKING")
        if values is None:
            values = self._property(self.root, "_NET_CLIENT_LIST")
        if values is not None:
            return [int(value) for value in values]
        try:
            return [int(window.id) for window in self.root.query_tree().children]
        except Exception as error:
            raise WindowCaptureError(
                f"Unable to enumerate X11 windows: {error}"
            ) from error

    def _title(self, window):
        value = self._property(
            window,
            "_NET_WM_NAME",
            self._atom("UTF8_STRING"),
        )
        if value is not None:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace").strip()
            return bytes(value).decode("utf-8", errors="replace").strip()
        try:
            return (window.get_wm_name() or "").strip()
        except Exception:
            return ""

    def _window_info(self, handle):
        try:
            window = self.display.create_resource_object("window", int(handle))
            title = self._title(window)
            attributes = window.get_attributes()
        except Exception:
            return None
        if not title:
            return None
        process_ids = self._property(window, "_NET_WM_PID")
        process_id = int(process_ids[0]) if process_ids is not None else 0
        states = self._property(window, "_NET_WM_STATE")
        hidden = self._atom("_NET_WM_STATE_HIDDEN")
        minimized = attributes.map_state != 2 or (
            states is not None and hidden in set(states)
        )
        return WindowInfo(int(handle), title, process_id, minimized)

    def list_windows(self):
        windows = [self._window_info(handle) for handle in self._client_ids()]
        windows = [
            window
            for window in windows
            if window is not None and window.process_id != os.getpid()
        ]
        return sorted(windows, key=lambda window: window.title.casefold())

    def get_window(self, handle):
        return self._window_info(handle)

    def get_client_geometry(self, handle):
        try:
            window = self.display.create_resource_object("window", int(handle))
            geometry = window.get_geometry()
            translated = window.translate_coords(self.root, 0, 0)
        except Exception as error:
            raise WindowNotFoundError(
                "Selected X11 game window is no longer available"
            ) from error
        if geometry.width <= 0 or geometry.height <= 0:
            raise WindowCaptureError("Selected game window has no visible client area")
        return WindowGeometry(
            int(translated.x),
            int(translated.y),
            int(geometry.width),
            int(geometry.height),
        )

    def get_foreground_handle(self):
        values = self._property(self.root, "_NET_ACTIVE_WINDOW")
        return int(values[0]) if values is not None and len(values) else 0


class WaylandWindowBackend:
    message = (
        "Capture is unavailable in native Wayland sessions. Log out and select "
        "an X11 desktop session before starting the application. Wayland blocks "
        "the global window enumeration, screen capture, and hotkeys this app needs."
    )

    def list_windows(self):
        raise WindowCaptureUnavailableError(self.message)

    def get_window(self, _handle):
        raise WindowCaptureUnavailableError(self.message)

    def get_client_geometry(self, _handle):
        raise WindowCaptureUnavailableError(self.message)

    def get_foreground_handle(self):
        raise WindowCaptureUnavailableError(self.message)


def is_native_wayland_session(*, platform=None, environment=None):
    platform = sys.platform if platform is None else platform
    environment = os.environ if environment is None else environment
    if not platform.startswith("linux"):
        return False
    session_type = environment.get("XDG_SESSION_TYPE", "").strip().casefold()
    return session_type == "wayland" or (
        bool(environment.get("WAYLAND_DISPLAY")) and session_type != "x11"
    )


def ensure_screen_capture_supported(*, platform=None, environment=None):
    if is_native_wayland_session(platform=platform, environment=environment):
        raise WindowCaptureUnavailableError(WaylandWindowBackend.message)


def create_window_backend(*, platform=None, environment=None):
    platform = sys.platform if platform is None else platform
    environment = os.environ if environment is None else environment
    if platform == "win32":
        return Win32WindowBackend()
    if platform == "darwin":
        return MacOSWindowBackend()
    if platform.startswith("linux"):
        if is_native_wayland_session(platform=platform, environment=environment):
            return WaylandWindowBackend()
        return LinuxX11WindowBackend(environment=environment)
    raise WindowCaptureUnavailableError(
        f"Game-window capture is unsupported on platform {platform!r}"
    )


class WindowCaptureTarget:
    def __init__(self, window_title, backend=None):
        self.window_title = (window_title or "").strip()
        self._backend = backend
        self._handle = None

    @property
    def backend(self):
        if self._backend is None:
            self._backend = create_window_backend()
        return self._backend

    def list_windows(self):
        return self.backend.list_windows()

    def get_geometry(self):
        window = self._resolve_window()
        if window.minimized:
            raise WindowMinimizedError(
                f"Selected game window {window.title!r} is minimized"
            )
        return self.backend.get_client_geometry(window.handle)

    def capture_box(self, region):
        return region.capture_box(self.get_geometry().as_monitor())

    def is_focused(self):
        window = self._resolve_window()
        process_probe = getattr(
            type(self.backend),
            "get_foreground_process_id",
            None,
        )
        if process_probe is not None:
            return int(process_probe(self.backend)) == int(window.process_id)
        return int(self.backend.get_foreground_handle()) == int(window.handle)

    def _resolve_window(self):
        if not self.window_title:
            raise WindowNotFoundError("Select a game window in Settings")

        if self._handle is not None:
            window = self.backend.get_window(self._handle)
            if window is not None and self._title_matches(window.title):
                return window
            self._handle = None

        windows = self.backend.list_windows()
        expected = self.window_title.casefold()
        window = next(
            (item for item in windows if item.title.casefold() == expected),
            None,
        )
        if window is None:
            window = next(
                (item for item in windows if self._title_matches(item.title)),
                None,
            )
        if window is None:
            raise WindowNotFoundError(
                f"Game window {self.window_title!r} is not available"
            )

        self._handle = window.handle
        return window

    def _title_matches(self, title):
        expected = self.window_title.casefold()
        actual = title.casefold()
        return expected == actual or expected in actual


def list_windows(backend=None):
    backend = backend or create_window_backend()
    return backend.list_windows()


def enable_windows_dpi_awareness(user32=None):
    if sys.platform != "win32" and user32 is None:
        return False
    user32 = user32 or ctypes.WinDLL("user32", use_last_error=True)
    try:
        set_awareness = user32.SetProcessDpiAwarenessContext
    except AttributeError:
        try:
            return bool(user32.SetProcessDPIAware())
        except AttributeError:
            return False

    set_awareness.argtypes = [ctypes.c_void_p]
    set_awareness.restype = ctypes.c_bool
    per_monitor_v2 = ctypes.c_void_p(-4)
    return bool(set_awareness(per_monitor_v2))

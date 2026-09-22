import ctypes
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


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

    def as_monitor(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


class Win32WindowBackend:
    def __init__(self, user32: object | None = None) -> None:
        if sys.platform != "win32" and user32 is None:
            raise WindowCaptureUnavailableError(
                "Game-window capture is available only on Windows"
            )

        from ctypes import wintypes

        self.wintypes = wintypes
        self.user32: object = user32 or getattr(ctypes, "WinDLL")(
            "user32", use_last_error=True
        )
        self.callback_type = getattr(ctypes, "WINFUNCTYPE")(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )
        self._configure_functions()

    @staticmethod
    def _integer(value: object) -> int:
        if isinstance(value, int | float | str | bytes | bytearray):
            return int(value)
        raise WindowCaptureError("Windows returned an invalid numeric window value")

    def _call(self, name: str, *arguments: object) -> object:
        function: object = getattr(self.user32, name)
        result: object = getattr(function, "__call__")(*arguments)
        return result

    def _configure_functions(self) -> None:
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
            function: object = getattr(self.user32, name)
            setattr(function, "argtypes", argument_types)
            setattr(function, "restype", result_type)

    def list_windows(self) -> list[WindowInfo]:
        windows: list[WindowInfo] = []

        def collect(handle: object, _parameter: object) -> bool:
            window = self.get_window(handle)
            if window is not None and window.process_id != os.getpid():
                windows.append(window)
            return True

        callback = self.callback_type(collect)
        if not self._call("EnumWindows", callback, 0):
            raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
        return sorted(windows, key=lambda window: window.title.casefold())

    def get_window(self, handle: object) -> WindowInfo | None:
        if not self._call("IsWindow", handle):
            return None
        if not self._call("IsWindowVisible", handle):
            return None
        title_length = self._integer(self._call("GetWindowTextLengthW", handle))
        if title_length <= 0:
            return None
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        if (
            self._integer(
                self._call("GetWindowTextW", handle, title_buffer, len(title_buffer))
            )
            <= 0
        ):
            return None
        title = title_buffer.value.strip()
        if not title:
            return None
        process_id = self.wintypes.DWORD()
        self._call("GetWindowThreadProcessId", handle, ctypes.byref(process_id))
        handle_value = getattr(handle, "value", handle)
        return WindowInfo(
            handle=self._integer(handle_value),
            title=title,
            process_id=process_id.value,
            minimized=bool(self._call("IsIconic", handle)),
        )

    def get_client_geometry(self, handle: int) -> WindowGeometry:
        rectangle = self.wintypes.RECT()
        if not self._call("GetClientRect", handle, ctypes.byref(rectangle)):
            raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())

        top_left = self.wintypes.POINT(rectangle.left, rectangle.top)
        bottom_right = self.wintypes.POINT(rectangle.right, rectangle.bottom)
        if not self._call("ClientToScreen", handle, ctypes.byref(top_left)):
            raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
        if not self._call("ClientToScreen", handle, ctypes.byref(bottom_right)):
            raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())

        width = bottom_right.x - top_left.x
        height = bottom_right.y - top_left.y
        if width <= 0 or height <= 0:
            raise WindowCaptureError("Selected game window has no visible client area")
        return WindowGeometry(top_left.x, top_left.y, width, height)

    def get_foreground_handle(self) -> int:
        handle = self._call("GetForegroundWindow")
        handle_value = getattr(handle, "value", handle)
        return self._integer(handle_value or 0)


class MacOSWindowBackend:
    def __init__(
        self, quartz: object | None = None, workspace: object | None = None
    ) -> None:
        if sys.platform != "darwin" and quartz is None:
            raise WindowCaptureUnavailableError(
                "macOS game-window capture is available only on macOS"
            )
        if quartz is None:
            try:
                import Quartz
            except ImportError as error:
                raise WindowCaptureUnavailableError(
                    "macOS window capture requires pyobjc-framework-Quartz"
                ) from error
            quartz = Quartz
        self.quartz = quartz
        self.workspace = workspace

    def _window_info(self, values: object) -> WindowInfo | None:
        quartz = self.quartz
        get_value = getattr(values, "get")
        layer = int(get_value(getattr(quartz, "kCGWindowLayer"), 0))
        bounds = get_value(getattr(quartz, "kCGWindowBounds")) or {}
        get_bound = getattr(bounds, "get")
        width = int(round(float(get_bound("Width", 0))))
        height = int(round(float(get_bound("Height", 0))))
        owner = str(get_value(getattr(quartz, "kCGWindowOwnerName")) or "").strip()
        name = str(get_value(getattr(quartz, "kCGWindowName")) or "").strip()
        if not owner or layer != 0 or width <= 0 or height <= 0:
            return None
        title = f"{owner} - {name}" if name and name != owner else owner
        return WindowInfo(
            handle=int(
                getattr(values, "__getitem__")(getattr(quartz, "kCGWindowNumber"))
            ),
            title=title,
            process_id=int(get_value(getattr(quartz, "kCGWindowOwnerPID"), 0)),
            minimized=not bool(
                get_value(getattr(quartz, "kCGWindowIsOnscreen"), False)
            ),
        )

    def _copy_windows(self, option: int, window_id: int = 0) -> Sequence[object]:
        windows: Sequence[object] = getattr(self.quartz, "CGWindowListCopyWindowInfo")(
            option, window_id
        )
        if windows is None:
            raise WindowCaptureError(
                "macOS did not return window information; allow Screen Recording "
                "access in System Settings"
            )
        return windows

    def list_windows(self) -> list[WindowInfo]:
        quartz = self.quartz
        values = self._copy_windows(
            getattr(quartz, "kCGWindowListOptionOnScreenOnly")
            | getattr(quartz, "kCGWindowListExcludeDesktopElements"),
            getattr(quartz, "kCGNullWindowID"),
        )
        windows: list[WindowInfo] = []
        for item in values:
            window = self._window_info(item)
            if window is not None and window.process_id != os.getpid():
                windows.append(window)
        return sorted(windows, key=lambda window: window.title.casefold())

    def get_window(self, handle: int) -> WindowInfo | None:
        quartz = self.quartz
        values = self._copy_windows(
            getattr(quartz, "kCGWindowListOptionIncludingWindow"),
            int(handle),
        )
        for item in values:
            if int(getattr(item, "get")(getattr(quartz, "kCGWindowNumber"), -1)) == int(
                handle
            ):
                return self._window_info(item)
        return None

    def get_client_geometry(self, handle: int) -> WindowGeometry:
        quartz = self.quartz
        values = self._copy_windows(
            getattr(quartz, "kCGWindowListOptionIncludingWindow"),
            int(handle),
        )
        for item in values:
            if int(getattr(item, "get")(getattr(quartz, "kCGWindowNumber"), -1)) != int(
                handle
            ):
                continue
            bounds = getattr(item, "get")(getattr(quartz, "kCGWindowBounds")) or {}
            get_bound = getattr(bounds, "get")
            geometry = WindowGeometry(
                left=int(round(float(get_bound("X", 0)))),
                top=int(round(float(get_bound("Y", 0)))),
                width=int(round(float(get_bound("Width", 0)))),
                height=int(round(float(get_bound("Height", 0)))),
            )
            if geometry.width <= 0 or geometry.height <= 0:
                raise WindowCaptureError(
                    "Selected game window has no visible capture area"
                )
            return geometry
        raise WindowNotFoundError("Selected macOS game window is no longer available")

    def get_foreground_handle(self) -> int:
        quartz = self.quartz
        values = self._copy_windows(
            getattr(quartz, "kCGWindowListOptionOnScreenOnly")
            | getattr(quartz, "kCGWindowListExcludeDesktopElements"),
            getattr(quartz, "kCGNullWindowID"),
        )
        for item in values:
            window = self._window_info(item)
            if window is not None:
                return window.handle
        return 0

    def get_foreground_process_id(self) -> int:
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
        application = getattr(workspace, "frontmostApplication")()
        if application is None:
            return 0
        return int(getattr(application, "processIdentifier")())


class LinuxX11WindowBackend:
    def __init__(
        self,
        display: object | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
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
        if display is None:
            raise WindowCaptureUnavailableError("Unable to connect to the X11 display")
        self.display = display
        self.root = getattr(display, "screen")().root
        self._atoms: dict[str, object] = {}

    def _atom(self, name: str) -> object:
        if name not in self._atoms:
            self._atoms[name] = getattr(self.display, "intern_atom")(name)
        return self._atoms[name]

    def _property(
        self, window: object, name: str, property_type: object = 0
    ) -> Sequence[object] | bytes | None:
        try:
            value = getattr(window, "get_full_property")(
                self._atom(name), property_type
            )
        except Exception:
            return None
        value = None if value is None else getattr(value, "value")
        if isinstance(value, bytes) or isinstance(value, Sequence):
            return value
        return None

    @staticmethod
    def _integer(value: object) -> int:
        if isinstance(value, int | float | str | bytes | bytearray):
            return int(value)
        raise WindowCaptureError("X11 returned an invalid numeric window property")

    def _client_ids(self) -> list[int]:
        values = self._property(self.root, "_NET_CLIENT_LIST_STACKING")
        if values is None:
            values = self._property(self.root, "_NET_CLIENT_LIST")
        if values is not None:
            return [self._integer(value) for value in values]
        try:
            return [
                self._integer(getattr(window, "id"))
                for window in getattr(self.root, "query_tree")().children
            ]
        except Exception as error:
            raise WindowCaptureError(
                f"Unable to enumerate X11 windows: {error}"
            ) from error

    def _title(self, window: object) -> str:
        value = self._property(
            window,
            "_NET_WM_NAME",
            self._atom("UTF8_STRING"),
        )
        if value is not None:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace").strip()
            return (
                bytes(self._integer(item) for item in value)
                .decode("utf-8", errors="replace")
                .strip()
            )
        try:
            return (getattr(window, "get_wm_name")() or "").strip()
        except Exception:
            return ""

    def _window_info(self, handle: int) -> WindowInfo | None:
        try:
            window = getattr(self.display, "create_resource_object")(
                "window", int(handle)
            )
            title = self._title(window)
            attributes = getattr(window, "get_attributes")()
        except Exception:
            return None
        if not title:
            return None
        process_ids = self._property(window, "_NET_WM_PID")
        process_id = self._integer(process_ids[0]) if process_ids is not None else 0
        states = self._property(window, "_NET_WM_STATE")
        hidden = self._atom("_NET_WM_STATE_HIDDEN")
        minimized = attributes.map_state != 2 or (
            states is not None and hidden in set(states)
        )
        return WindowInfo(int(handle), title, process_id, minimized)

    def list_windows(self) -> list[WindowInfo]:
        windows: list[WindowInfo] = []
        for handle in self._client_ids():
            window = self._window_info(handle)
            if window is not None and window.process_id != os.getpid():
                windows.append(window)
        return sorted(windows, key=lambda window: window.title.casefold())

    def get_window(self, handle: int) -> WindowInfo | None:
        return self._window_info(handle)

    def get_client_geometry(self, handle: int) -> WindowGeometry:
        try:
            window = getattr(self.display, "create_resource_object")(
                "window", int(handle)
            )
            geometry = getattr(window, "get_geometry")()
            translated = getattr(window, "translate_coords")(self.root, 0, 0)
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

    def get_foreground_handle(self) -> int:
        values = self._property(self.root, "_NET_ACTIVE_WINDOW")
        return self._integer(values[0]) if values is not None and len(values) else 0


class WaylandWindowBackend:
    message = (
        "Capture is unavailable in native Wayland sessions. Log out and select "
        "an X11 desktop session before starting the application. Wayland blocks "
        "the global window enumeration, screen capture, and hotkeys this app needs."
    )

    def list_windows(self) -> list[WindowInfo]:
        raise WindowCaptureUnavailableError(self.message)

    def get_window(self, _handle: int) -> WindowInfo | None:
        raise WindowCaptureUnavailableError(self.message)

    def get_client_geometry(self, _handle: int) -> WindowGeometry:
        raise WindowCaptureUnavailableError(self.message)

    def get_foreground_handle(self) -> int:
        raise WindowCaptureUnavailableError(self.message)


def is_native_wayland_session(
    *, platform: str | None = None, environment: Mapping[str, str] | None = None
) -> bool:
    platform = sys.platform if platform is None else platform
    environment = os.environ if environment is None else environment
    if not platform.startswith("linux"):
        return False
    session_type = environment.get("XDG_SESSION_TYPE", "").strip().casefold()
    return session_type == "wayland" or (
        bool(environment.get("WAYLAND_DISPLAY")) and session_type != "x11"
    )


def ensure_screen_capture_supported(
    *, platform: str | None = None, environment: Mapping[str, str] | None = None
) -> None:
    if is_native_wayland_session(platform=platform, environment=environment):
        raise WindowCaptureUnavailableError(WaylandWindowBackend.message)


class _WindowBackend(Protocol):
    def list_windows(self) -> list[WindowInfo]: ...

    def get_window(self, handle: int) -> WindowInfo | None: ...

    def get_client_geometry(self, handle: int) -> WindowGeometry: ...

    def get_foreground_handle(self) -> int: ...


class _CaptureRegion(Protocol):
    def capture_box(self, monitor: dict[str, int]) -> dict[str, int]: ...


def create_window_backend(
    *, platform: str | None = None, environment: Mapping[str, str] | None = None
) -> _WindowBackend:
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
    def __init__(
        self, window_title: str | None, backend: _WindowBackend | None = None
    ) -> None:
        self.window_title = (window_title or "").strip()
        self._backend = backend
        self._handle: int | None = None

    @property
    def backend(self) -> _WindowBackend:
        if self._backend is None:
            self._backend = create_window_backend()
        return self._backend

    def list_windows(self) -> list[WindowInfo]:
        return self.backend.list_windows()

    def get_geometry(self) -> WindowGeometry:
        window = self._resolve_window()
        if window.minimized:
            raise WindowMinimizedError(
                f"Selected game window {window.title!r} is minimized"
            )
        return self.backend.get_client_geometry(window.handle)

    def capture_box(self, region: _CaptureRegion) -> dict[str, int]:
        return region.capture_box(self.get_geometry().as_monitor())

    def is_focused(self) -> bool:
        window = self._resolve_window()
        process_probe = getattr(
            type(self.backend),
            "get_foreground_process_id",
            None,
        )
        if process_probe is not None:
            return int(process_probe(self.backend)) == int(window.process_id)
        return int(self.backend.get_foreground_handle()) == int(window.handle)

    def _resolve_window(self) -> WindowInfo:
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

    def _title_matches(self, title: str) -> bool:
        expected = self.window_title.casefold()
        actual = title.casefold()
        return expected == actual or expected in actual


def list_windows(backend: _WindowBackend | None = None) -> list[WindowInfo]:
    backend = backend or create_window_backend()
    return backend.list_windows()


def enable_windows_dpi_awareness(user32: object | None = None) -> bool:
    if sys.platform != "win32" and user32 is None:
        return False
    user32 = user32 or getattr(ctypes, "WinDLL")("user32", use_last_error=True)
    try:
        set_awareness = getattr(user32, "SetProcessDpiAwarenessContext")
    except AttributeError:
        try:
            return bool(getattr(user32, "SetProcessDPIAware")())
        except AttributeError:
            return False

    set_awareness.argtypes = [ctypes.c_void_p]
    set_awareness.restype = ctypes.c_bool
    per_monitor_v2 = ctypes.c_void_p(-4)
    return bool(set_awareness(per_monitor_v2))

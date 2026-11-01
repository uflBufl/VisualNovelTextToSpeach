import ctypes
import sys
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from pynput import keyboard

from vntts.cleanup import attempt_cleanup

advance_keys = {
    "space": keyboard.Key.space,
    "enter": keyboard.Key.enter,
    "right": keyboard.Key.right,
    "down": keyboard.Key.down,
}
macos_virtual_keys = {
    "space": 49,
    "enter": 36,
    "right": 124,
    "down": 125,
}
windows_virtual_keys = {
    "space": 0x20,
    "enter": 0x0D,
    "right": 0x27,
    "down": 0x28,
}


class KeyboardController(Protocol):
    def press(self, key: object) -> object: ...

    def release(self, key: object) -> object: ...


@runtime_checkable
class WindowsInput(Protocol):
    def SendInput(self, count: int, inputs: object, size: int) -> int: ...


class QuartzInput(Protocol):
    kCGHIDEventTap: object

    def CGEventCreateKeyboardEvent(
        self, source: object | None, keycode: int, pressed: bool
    ) -> object | None: ...

    def CGEventPost(self, tap: object, event: object) -> object: ...


def _windows_input() -> WindowsInput:
    libraries = getattr(ctypes, "windll", None)
    user32 = getattr(libraries, "user32", None)
    if not isinstance(user32, WindowsInput):
        raise OSError("Windows user32 input API is unavailable")
    return user32


def send_windows_key(
    keycode: int,
    *,
    user32: WindowsInput | None = None,
    _diagnostics: dict[str, object] | None = None,
    _diagnostic_probe: Callable[[], dict[str, object]] | None = None,
) -> bool:
    class KeyboardInput(ctypes.Structure):
        _fields_ = (
            ("virtual_key", ctypes.c_ushort),
            ("scan_code", ctypes.c_ushort),
            ("flags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("extra_info", ctypes.c_size_t),
        )

    class MouseInput(ctypes.Structure):
        _fields_ = (
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouse_data", ctypes.c_ulong),
            ("flags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("extra_info", ctypes.c_size_t),
        )

    class HardwareInput(ctypes.Structure):
        _fields_ = (
            ("message", ctypes.c_ulong),
            ("parameter_low", ctypes.c_ushort),
            ("parameter_high", ctypes.c_ushort),
        )

    class InputValue(ctypes.Union):
        _fields_ = (
            ("keyboard", KeyboardInput),
            ("mouse", MouseInput),
            ("hardware", HardwareInput),
        )

    class Input(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = (("type", ctypes.c_ulong), ("value", InputValue))

    input_keyboard = 1
    key_up = 0x0002
    events = (Input * 2)(
        Input(input_keyboard, InputValue(keyboard=KeyboardInput(keycode, 0, 0, 0, 0))),
        Input(
            input_keyboard,
            InputValue(keyboard=KeyboardInput(keycode, 0, key_up, 0, 0)),
        ),
    )
    user32 = user32 or _windows_input()
    _collect_input_probe(_diagnostics, _diagnostic_probe)
    if _diagnostics is not None:
        _diagnostics["input_events_requested"] = len(events)

    def send_batch() -> int:
        sent = user32.SendInput(len(events), ctypes.byref(events), ctypes.sizeof(Input))
        if sent != len(events) and _diagnostics is not None:
            get_last_error = getattr(ctypes, "GetLastError", None)
            if get_last_error is not None:
                _diagnostics["native_error_code"] = get_last_error()
        return sent

    sent = _record_input_call(
        send_batch,
        "batch",
        _diagnostics,
    )
    if _diagnostics is not None:
        _diagnostics["input_events_sent"] = sent
    if sent != len(events):
        cleanup_sent = _record_input_call(
            lambda: user32.SendInput(1, ctypes.byref(events[1]), ctypes.sizeof(Input)),
            "release",
            _diagnostics,
        )
        if _diagnostics is not None:
            _diagnostics["cleanup_events_sent"] = cleanup_sent
        raise OSError("Windows SendInput could not post the auto-advance key")
    return True


def _record_input_call[T](
    operation: Callable[[], T],
    phase: str,
    diagnostics: dict[str, object] | None,
) -> T:
    if diagnostics is None:
        return operation()
    diagnostics[f"{phase}_started_monotonic"] = time.monotonic()
    try:
        return operation()
    except BaseException as error:
        diagnostics[f"{phase}_error_type"] = type(error).__name__
        raise
    finally:
        diagnostics[f"{phase}_completed_monotonic"] = time.monotonic()


def _send_key_pair(
    press: Callable[[], object],
    release: Callable[[], object],
    diagnostics: dict[str, object] | None = None,
    diagnostic_probe: Callable[[], dict[str, object]] | None = None,
) -> None:
    def release_key() -> object:
        return _record_input_call(release, "release", diagnostics)

    _collect_input_probe(diagnostics, diagnostic_probe)
    try:
        _record_input_call(press, "press", diagnostics)
    except BaseException as error:
        attempt_cleanup(
            release_key,
            description="Auto-advance key release",
            primary_error=error,
        )
        raise
    else:
        release_key()


def _collect_input_probe(
    diagnostics: dict[str, object] | None,
    probe: Callable[[], dict[str, object]] | None,
) -> None:
    if diagnostics is None or probe is None:
        return
    try:
        diagnostics.update(probe())
    except BaseException as error:
        diagnostics["before_focus_error_type"] = type(error).__name__


def _report_diagnostic(
    handler: Callable[[dict[str, object]], object], diagnostics: dict[str, object]
) -> None:
    try:
        handler(diagnostics)
    except BaseException:
        # Diagnostics must never change the input result or its original error.
        pass


class DialogueAdvancer:
    """Send one conservative dialogue-advance key press."""

    def __init__(
        self,
        key: str = "space",
        *,
        controller_factory: Callable[[], KeyboardController] = keyboard.Controller,
        platform: str | None = None,
        quartz_module: QuartzInput | None = None,
        accessibility_probe: Callable[[], object] | None = None,
        windows_sender: Callable[[int], bool] | None = None,
        diagnostic_handler: Callable[[dict[str, object]], object] | None = None,
        diagnostic_probe: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        if key not in advance_keys:
            choices = ", ".join(sorted(advance_keys))
            raise ValueError(f"Unknown auto-advance key {key!r}; choose {choices}")
        self.key = key
        self.controller_factory = controller_factory
        self.platform = sys.platform if platform is None else platform
        self.quartz_module = quartz_module
        self.accessibility_probe = accessibility_probe
        self.windows_sender = windows_sender or send_windows_key
        self.diagnostic_handler = diagnostic_handler
        self.diagnostic_probe = diagnostic_probe

    def advance(self) -> bool:
        mechanism = "pynput"
        if self.platform == "darwin":
            mechanism = "quartz"
        elif self.platform == "win32":
            mechanism = (
                "sendinput" if self.windows_sender is send_windows_key else "custom"
            )
        diagnostics: dict[str, object] = {
            "started_monotonic": time.monotonic(),
            "mechanism": mechanism,
            "input_key": self.key,
            "success": False,
        }
        if self.platform in ("darwin", "win32"):
            diagnostics["key_code"] = (
                macos_virtual_keys
                if self.platform == "darwin"
                else windows_virtual_keys
            )[self.key]
        try:
            success = self._advance(diagnostics)
            diagnostics["success"] = success
            return success
        except BaseException as error:
            diagnostics["error_type"] = type(error).__name__
            raise
        finally:
            diagnostics["completed_monotonic"] = time.monotonic()
            if self.diagnostic_handler is not None:
                _report_diagnostic(self.diagnostic_handler, diagnostics)

    def _advance(self, diagnostics: dict[str, object]) -> bool:
        if self.platform == "darwin":
            return self._advance_macos(diagnostics)
        if self.platform == "win32":
            if self.windows_sender is send_windows_key:
                return send_windows_key(
                    windows_virtual_keys[self.key],
                    _diagnostics=diagnostics,
                    _diagnostic_probe=self.diagnostic_probe,
                )
            _collect_input_probe(diagnostics, self.diagnostic_probe)
            return self.windows_sender(windows_virtual_keys[self.key])
        controller = self.controller_factory()
        key = advance_keys[self.key]
        _send_key_pair(
            lambda: controller.press(key),
            lambda: controller.release(key),
            diagnostics,
            self.diagnostic_probe,
        )
        return True

    def _advance_macos(self, diagnostics: dict[str, object]) -> bool:
        # pynput asks Text Input Services for the current keyboard layout.
        # macOS asserts when that API is reached from live mode's timer thread.
        # Quartz posts virtual-key events directly and is safe from this worker.
        quartz = self.quartz_module
        if quartz is None:
            accessibility_probe = self.accessibility_probe
            if accessibility_probe is None:
                from ApplicationServices import AXIsProcessTrusted

                accessibility_probe = AXIsProcessTrusted
            if not accessibility_probe():
                raise PermissionError(
                    "macOS Accessibility permission is required for auto advance"
                )
            import Quartz

            quartz = Quartz
        keycode = macos_virtual_keys[self.key]
        events = tuple(
            quartz.CGEventCreateKeyboardEvent(None, keycode, pressed)
            for pressed in (True, False)
        )
        if any(event is None for event in events):
            raise RuntimeError("macOS could not create an auto-advance key event")
        _send_key_pair(
            lambda: quartz.CGEventPost(quartz.kCGHIDEventTap, events[0]),
            lambda: quartz.CGEventPost(quartz.kCGHIDEventTap, events[1]),
            diagnostics,
            self.diagnostic_probe,
        )
        return True

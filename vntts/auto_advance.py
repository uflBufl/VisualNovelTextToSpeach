import ctypes
import sys

from pynput import keyboard

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


def send_windows_key(keycode, *, user32=None):
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
    user32 = user32 or ctypes.windll.user32
    sent = user32.SendInput(len(events), ctypes.byref(events), ctypes.sizeof(Input))
    if sent != len(events):
        raise OSError("Windows SendInput could not post the auto-advance key")
    return True


class DialogueAdvancer:
    """Send one conservative dialogue-advance key press."""

    def __init__(
        self,
        key="space",
        *,
        controller_factory=keyboard.Controller,
        platform=None,
        quartz_module=None,
        accessibility_probe=None,
        windows_sender=None,
    ):
        if key not in advance_keys:
            choices = ", ".join(sorted(advance_keys))
            raise ValueError(f"Unknown auto-advance key {key!r}; choose {choices}")
        self.key = key
        self.controller_factory = controller_factory
        self.platform = sys.platform if platform is None else platform
        self.quartz_module = quartz_module
        self.accessibility_probe = accessibility_probe
        self.windows_sender = windows_sender or send_windows_key

    def advance(self):
        if self.platform == "darwin":
            return self._advance_macos()
        if self.platform == "win32":
            return self.windows_sender(windows_virtual_keys[self.key])
        controller = self.controller_factory()
        controller.press(advance_keys[self.key])
        controller.release(advance_keys[self.key])
        return True

    def _advance_macos(self):
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
        for pressed in (True, False):
            event = quartz.CGEventCreateKeyboardEvent(None, keycode, pressed)
            if event is None:
                raise RuntimeError("macOS could not create an auto-advance key event")
            quartz.CGEventPost(quartz.kCGHIDEventTap, event)
        return True

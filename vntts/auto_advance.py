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


class DialogueAdvancer:
    """Send one conservative dialogue-advance key press."""

    def __init__(
        self,
        key="space",
        *,
        controller_factory=keyboard.Controller,
        platform=None,
        quartz_module=None,
    ):
        if key not in advance_keys:
            choices = ", ".join(sorted(advance_keys))
            raise ValueError(f"Unknown auto-advance key {key!r}; choose {choices}")
        self.key = key
        self.controller_factory = controller_factory
        self.platform = sys.platform if platform is None else platform
        self.quartz_module = quartz_module

    def advance(self):
        if self.platform == "darwin":
            return self._advance_macos()
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
            import Quartz

            quartz = Quartz
        keycode = macos_virtual_keys[self.key]
        for pressed in (True, False):
            event = quartz.CGEventCreateKeyboardEvent(None, keycode, pressed)
            if event is None:
                raise RuntimeError("macOS could not create an auto-advance key event")
            quartz.CGEventPost(quartz.kCGHIDEventTap, event)
        return True

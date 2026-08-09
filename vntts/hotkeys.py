import sys

from pynput import keyboard


class HotkeyValidationError(ValueError):
    pass


_modifier_keys = {
    keyboard.Key.alt,
    keyboard.Key.alt_l,
    keyboard.Key.alt_r,
    keyboard.Key.alt_gr,
    keyboard.Key.ctrl,
    keyboard.Key.ctrl_l,
    keyboard.Key.ctrl_r,
    keyboard.Key.cmd,
    keyboard.Key.cmd_l,
    keyboard.Key.cmd_r,
    keyboard.Key.shift,
    keyboard.Key.shift_l,
    keyboard.Key.shift_r,
}


def default_hotkey(key, *, platform=None):
    platform = sys.platform if platform is None else platform
    primary_modifier = "<cmd>" if platform == "darwin" else "<ctrl>"
    return f"{primary_modifier}+<shift>+{key}"


def validate_hotkey_assignments(assignments, *, platform=None):
    platform = sys.platform if platform is None else platform
    parsed_assignments = {}
    for label, hotkey in assignments.items():
        try:
            parsed = keyboard.HotKey.parse(hotkey)
        except (TypeError, ValueError) as error:
            raise HotkeyValidationError(f"{label}: {error}") from error
        regular_keys = [key for key in parsed if key not in _modifier_keys]
        if len(regular_keys) != 1 or len(parsed) == 1:
            raise HotkeyValidationError(
                f"{label}: press modifiers together with one regular key"
            )
        parsed_assignments[label] = frozenset(parsed)

    seen = {}
    for label, parsed in parsed_assignments.items():
        if parsed in seen:
            raise HotkeyValidationError(
                f"{label} duplicates the shortcut used by {seen[parsed]}"
            )
        seen[parsed] = label

    reserved = {
        frozenset(keyboard.HotKey.parse(hotkey))
        for hotkey in _reserved_hotkeys(platform)
    }
    for label, parsed in parsed_assignments.items():
        if parsed in reserved:
            raise HotkeyValidationError(
                f"{label}: this shortcut is reserved by the operating system"
            )


def _reserved_hotkeys(platform):
    if platform == "darwin":
        return (
            "<cmd>+q",
            "<cmd>+w",
            "<cmd>+<space>",
            "<cmd>+<tab>",
        )
    return (
        "<alt>+<f4>",
        "<ctrl>+<alt>+<delete>",
    )

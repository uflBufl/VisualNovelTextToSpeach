import sys

from PySide6.QtCore import QKeyCombination, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QKeySequenceEdit

from vntts.hotkeys import HotkeyValidationError

_special_keys = {
    Qt.Key.Key_Backspace.value: "<backspace>",
    Qt.Key.Key_Delete.value: "<delete>",
    Qt.Key.Key_Down.value: "<down>",
    Qt.Key.Key_End.value: "<end>",
    Qt.Key.Key_Enter.value: "<enter>",
    Qt.Key.Key_Escape.value: "<esc>",
    Qt.Key.Key_Home.value: "<home>",
    Qt.Key.Key_Insert.value: "<insert>",
    Qt.Key.Key_Left.value: "<left>",
    Qt.Key.Key_PageDown.value: "<page_down>",
    Qt.Key.Key_PageUp.value: "<page_up>",
    Qt.Key.Key_Return.value: "<enter>",
    Qt.Key.Key_Right.value: "<right>",
    Qt.Key.Key_Space.value: "<space>",
    Qt.Key.Key_Tab.value: "<tab>",
    Qt.Key.Key_Up.value: "<up>",
}

_qt_special_keys = {
    "backspace": "Backspace",
    "delete": "Del",
    "down": "Down",
    "end": "End",
    "enter": "Return",
    "esc": "Esc",
    "home": "Home",
    "insert": "Ins",
    "left": "Left",
    "page_down": "PgDown",
    "page_up": "PgUp",
    "right": "Right",
    "space": "Space",
    "tab": "Tab",
    "up": "Up",
}


class HotkeyRecorder(QKeySequenceEdit):
    def __init__(self, hotkey, parent=None, *, platform=None):
        super().__init__(parent)
        self.platform = sys.platform if platform is None else platform
        self.setMaximumSequenceLength(1)
        self.setClearButtonEnabled(True)
        self.setToolTip("Click, then press the complete shortcut")
        try:
            self.set_hotkey(hotkey)
        except HotkeyValidationError:
            self.clear()

    def hotkey(self):
        return hotkey_from_qt_sequence(self.keySequence(), platform=self.platform)

    def set_hotkey(self, hotkey):
        self.setKeySequence(qt_sequence_from_hotkey(hotkey, platform=self.platform))


def hotkey_from_qt_sequence(sequence, *, platform=None):
    platform = sys.platform if platform is None else platform
    if sequence.isEmpty():
        raise HotkeyValidationError("press a shortcut")
    combination = sequence[0]
    modifiers = combination.keyboardModifiers()
    tokens = []
    if platform == "darwin":
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            tokens.append("<cmd>")
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            tokens.append("<ctrl>")
    else:
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            tokens.append("<ctrl>")
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            tokens.append("<cmd>")
    if modifiers & Qt.KeyboardModifier.AltModifier:
        tokens.append("<alt>")
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        tokens.append("<shift>")

    key_value = combination.key().value
    key = _key_token(key_value)
    if key is None:
        raise HotkeyValidationError("this key is unavailable for a global shortcut")
    tokens.append(key)
    return "+".join(tokens)


def qt_sequence_from_hotkey(hotkey, *, platform=None):
    platform = sys.platform if platform is None else platform
    components = hotkey.casefold().split("+")
    if not components or not components[-1]:
        raise HotkeyValidationError("invalid shortcut")
    qt_components = []
    for component in components[:-1]:
        if component == "<ctrl>":
            qt_components.append("Meta" if platform == "darwin" else "Ctrl")
        elif component == "<cmd>":
            qt_components.append("Ctrl" if platform == "darwin" else "Meta")
        elif component == "<alt>":
            qt_components.append("Alt")
        elif component == "<shift>":
            qt_components.append("Shift")
        else:
            raise HotkeyValidationError(f"unsupported modifier {component}")

    key = components[-1]
    if key.startswith("<") and key.endswith(">"):
        name = key[1:-1]
        if name.startswith("f") and name[1:].isdigit():
            qt_key = name.upper()
        else:
            qt_key = _qt_special_keys.get(name)
        if qt_key is None:
            raise HotkeyValidationError(f"unsupported key {key}")
    elif len(key) == 1 and key != "+":
        qt_key = key.upper() if key.isalpha() else key
    else:
        raise HotkeyValidationError(f"unsupported key {key}")

    sequence = QKeySequence("+".join((*qt_components, qt_key)))
    if sequence.isEmpty():
        raise HotkeyValidationError("invalid shortcut")
    return sequence


def _key_token(key_value):
    if Qt.Key.Key_A.value <= key_value <= Qt.Key.Key_Z.value:
        return chr(key_value).casefold()
    if Qt.Key.Key_0.value <= key_value <= Qt.Key.Key_9.value:
        return chr(key_value)
    if Qt.Key.Key_F1.value <= key_value <= Qt.Key.Key_F35.value:
        return f"<f{key_value - Qt.Key.Key_F1.value + 1}>"
    if key_value in _special_keys:
        return _special_keys[key_value]

    key_text = QKeySequence(
        QKeyCombination(Qt.KeyboardModifier.NoModifier, Qt.Key(key_value))
    ).toString(QKeySequence.SequenceFormat.PortableText)
    if len(key_text) == 1 and key_text != "+":
        return key_text.casefold()
    return None

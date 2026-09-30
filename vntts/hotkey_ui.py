import sys
from typing import Protocol, runtime_checkable

from PySide6.QtCore import QKeyCombination, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QKeySequenceEdit, QWidget

from vntts.hotkeys import HotkeyValidationError


@runtime_checkable
class _KeySequenceIndex(Protocol):
    def __getitem__(self, index: int) -> QKeyCombination: ...


def _first_combination(sequence: QKeySequence) -> QKeyCombination:
    if isinstance(sequence, _KeySequenceIndex):
        return sequence[0]
    raise HotkeyValidationError("this Qt build cannot inspect shortcuts")


_qt_key_tokens = {
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
    Qt.Key.Key_CapsLock.value: "<caps_lock>",
    Qt.Key.Key_MediaNext.value: "<media_next>",
    Qt.Key.Key_MediaPrevious.value: "<media_previous>",
    Qt.Key.Key_MediaTogglePlayPause.value: "<media_play_pause>",
    Qt.Key.Key_VolumeDown.value: "<media_volume_down>",
    Qt.Key.Key_VolumeMute.value: "<media_volume_mute>",
    Qt.Key.Key_VolumeUp.value: "<media_volume_up>",
}
_token_qt_keys = {token: Qt.Key(value) for value, token in _qt_key_tokens.items()}


class HotkeyRecorder(QKeySequenceEdit):
    def __init__(
        self,
        hotkey: str,
        parent: QWidget | None = None,
        *,
        platform: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.platform = sys.platform if platform is None else platform
        self.setMaximumSequenceLength(1)
        self.setClearButtonEnabled(True)
        self.setToolTip("Click, then press the complete shortcut")
        try:
            self.set_hotkey(hotkey)
        except HotkeyValidationError:
            self.clear()

    def hotkey(self) -> str:
        return hotkey_from_qt_sequence(self.keySequence(), platform=self.platform)

    def set_hotkey(self, hotkey: str) -> None:
        self.setKeySequence(qt_sequence_from_hotkey(hotkey, platform=self.platform))


def _qt_modifier_tokens(platform: str) -> dict[Qt.KeyboardModifier, str]:
    return {
        Qt.KeyboardModifier.ControlModifier: "<cmd>"
        if platform == "darwin"
        else "<ctrl>",
        Qt.KeyboardModifier.MetaModifier: "<ctrl>" if platform == "darwin" else "<cmd>",
        Qt.KeyboardModifier.AltModifier: "<alt>",
        Qt.KeyboardModifier.ShiftModifier: "<shift>",
    }


def hotkey_from_qt_sequence(
    sequence: QKeySequence, *, platform: str | None = None
) -> str:
    platform = sys.platform if platform is None else platform
    if sequence.count() != 1:
        raise HotkeyValidationError("press a shortcut")
    combination = _first_combination(sequence)
    modifiers = combination.keyboardModifiers()
    tokens: list[str] = []
    for modifier, token in _qt_modifier_tokens(platform).items():
        if modifiers & modifier:
            tokens.append(token)

    key_value = combination.key().value
    key = _key_token(key_value)
    if key is None:
        raise HotkeyValidationError("this key is unavailable for a global shortcut")
    tokens.append(key)
    return "+".join(tokens)


def qt_sequence_from_hotkey(
    hotkey: str, *, platform: str | None = None
) -> QKeySequence:
    platform = sys.platform if platform is None else platform
    components = hotkey.casefold().split("+")
    if components[-2:] == ["", ""]:
        components[-2:] = ["+"]
    if not components[-1] or any(not token for token in components[:-1]):
        raise HotkeyValidationError("invalid shortcut")
    modifier_tokens = {
        token: modifier for modifier, token in _qt_modifier_tokens(platform).items()
    }
    modifiers = Qt.KeyboardModifier.NoModifier
    for component in components[:-1]:
        try:
            modifiers |= modifier_tokens[component]
        except KeyError as error:
            raise HotkeyValidationError(f"unsupported modifier {component}") from error
    qt_key = _qt_key_from_token(components[-1])

    return QKeySequence(QKeyCombination(modifiers, qt_key))


def _qt_key_from_token(key: str) -> Qt.Key:
    if key == "+":
        return Qt.Key.Key_Plus
    if key.startswith("<") and key.endswith(">"):
        name = key[1:-1]
        if name.startswith("f") and name[1:].isdecimal():
            function_number = int(name[1:])
            if 1 <= function_number <= 35:
                return Qt.Key(Qt.Key.Key_F1.value + function_number - 1)
        qt_key = _token_qt_keys.get(key)
        if qt_key is not None:
            return qt_key
    elif len(key) == 1:
        upper_key = key.upper()
        if len(upper_key) == 1:
            return Qt.Key(ord(upper_key) if key.isalpha() else ord(key))
    raise HotkeyValidationError(f"unsupported key {key}")


def _key_token(key_value: int) -> str | None:
    if Qt.Key.Key_A.value <= key_value <= Qt.Key.Key_Z.value:
        return chr(key_value).casefold()
    if Qt.Key.Key_0.value <= key_value <= Qt.Key.Key_9.value:
        return chr(key_value)
    if Qt.Key.Key_F1.value <= key_value <= Qt.Key.Key_F35.value:
        return f"<f{key_value - Qt.Key.Key_F1.value + 1}>"
    if key_value in _qt_key_tokens:
        return _qt_key_tokens[key_value]

    key_text = QKeySequence(
        QKeyCombination(Qt.KeyboardModifier.NoModifier, Qt.Key(key_value))
    ).toString(QKeySequence.SequenceFormat.PortableText)
    if len(key_text) == 1:
        return key_text.casefold()
    return None

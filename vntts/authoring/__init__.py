"""Small public API for offline authoring."""

from __future__ import annotations

import importlib

_EXPORTS = {
    "MissingVoicePolicy": "vntts.authoring.missing_voice_policy",
    "NARRATOR_ROLES": "vntts.authoring.missing_voice_policy",
    "publish_final_game_pack": "vntts.authoring.game_pack",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> object:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value

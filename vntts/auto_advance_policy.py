"""Shared capture authority policy for auto-advance controls."""


def auto_advance_allowed(capture_mode, sequence_mode):
    return capture_mode == "window" and sequence_mode != "audio-manual"


def auto_advance_control_state(capture_mode, sequence_mode, checked):
    allowed = auto_advance_allowed(capture_mode, sequence_mode)
    if capture_mode != "window":
        tooltip = (
            "Auto advance requires a selected game window so focus can be verified."
        )
    elif sequence_mode == "audio-manual":
        tooltip = (
            "Sequence-first canonical routing never sends advance keys in the "
            "manual recovery mode."
        )
    elif sequence_mode == "audio-auto":
        tooltip = (
            "Guarded sequence control sends at most one key for the current "
            "automatic event, only while the selected game window is focused and "
            "its dialogue frame remains visible and stable."
        )
    else:
        tooltip = ""
    return allowed, bool(checked and allowed), tooltip


def guard_auto_advance_settings(settings):
    """Clear impossible auto-advance state at settings publication boundaries."""
    _allowed, enabled, _reason = auto_advance_control_state(
        settings.capture_mode,
        settings.live_sequence_mode,
        settings.auto_advance_enabled,
    )
    if enabled == settings.auto_advance_enabled:
        return settings
    return settings.updated(auto_advance_enabled=enabled)

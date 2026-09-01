"""Shared capture authority policy for auto-advance controls."""


def auto_advance_control_state(capture_mode, sequence_mode, checked):
    window_capture = capture_mode == "window"
    manual_sequence = sequence_mode == "audio-manual"
    allowed = window_capture and not manual_sequence
    if not window_capture:
        tooltip = (
            "Auto advance requires a selected game window so focus can be verified."
        )
    elif manual_sequence:
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

"""Metadata about actual capture attempts; never includes captured pixels or text."""

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass

CAPTURE_GEOMETRY_FIELDS = frozenset(
    {"window_bounds", "capture_bounds", "output_dimensions"}
)
_RECTANGLE = ("left", "top", "width", "height")


def sanitize_capture_geometry(key: str, value: object) -> object:
    # Older Reading snapshots stored Python's string representation.
    if isinstance(value, str) and len(value) <= 256:
        try:
            value = json.loads(
                value.replace("'", '"').replace("(", "[").replace(")", "]")
            )
        except ValueError:
            return None
    bounds = key != "output_dimensions"
    if bounds and isinstance(value, Mapping):
        values = [value.get(field) for field in _RECTANGLE]
    elif isinstance(value, (tuple, list)) and len(value) == (4 if bounds else 2):
        values = list(value)
    else:
        return None
    if not all(
        type(item) is int
        and item.bit_length() <= 64
        or isinstance(item, float)
        and math.isfinite(item)
        for item in values
    ):
        return None
    return dict(zip(_RECTANGLE, values, strict=True)) if bounds else values


@dataclass(frozen=True)
class CaptureProvenance:
    capture_id: str
    run_id: str | None
    started_monotonic: float
    completed_monotonic: float
    requested_window_title: str | None
    window_id: int | None
    window_title: str | None
    owner_pid: int | None
    window_bounds: dict[str, int] | None
    capture_bounds: dict[str, int] | None
    output_dimensions: tuple[int, int] | None
    success: bool
    error_type: str | None = None
    mechanism: str = "native"
    scope: str = "selected_window"
    coordinate_space: str = "canonical_content_pixels_v1"
    session_id: str | None = None
    frame_sequence: int | None = None
    layout_epoch: int | None = None
    acquired_monotonic: float | None = None
    source_monotonic: float | None = None
    clock_uncertainty_ms: float | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

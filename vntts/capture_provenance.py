"""Metadata about actual capture attempts; never includes captured pixels or text."""

from dataclasses import asdict, dataclass


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
    mechanism: str = "mss"
    # MSS reads desktop pixels at these coordinates, including overlapping windows.
    scope: str = "desktop_rectangle"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

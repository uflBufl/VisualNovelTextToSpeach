import math
from typing import SupportsFloat, SupportsIndex


def optional_finite_scalar(value: object) -> float | None:
    """Admit numeric float/index values, including native scalars and bools."""
    if not isinstance(value, (SupportsFloat, SupportsIndex)):
        return None
    try:
        number = float(value)
    except TypeError, ValueError, OverflowError:
        return None
    return number if math.isfinite(number) else None


def optional_finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def optional_milliseconds_seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        seconds = value / 1000
    except OverflowError:
        return None
    return optional_finite_number(seconds)

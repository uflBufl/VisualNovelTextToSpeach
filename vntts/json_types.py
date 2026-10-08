"""Small runtime-neutral predicates for JSON-shaped values."""

from typing import TypeGuard


def is_json_object(value: object) -> TypeGuard[dict[str, object]]:
    """Check that a value is a dictionary with string keys."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


__all__ = ["is_json_object"]

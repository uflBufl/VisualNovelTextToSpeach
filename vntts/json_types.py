"""Small runtime-neutral predicates for JSON-shaped values."""

from collections.abc import Mapping
from typing import TypeGuard


def is_json_object(value: object) -> TypeGuard[dict[str, object]]:
    """Check that a value is a dictionary with string keys."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def has_schema_version(document: Mapping[str, object], expected: int) -> bool:
    """Match a schema version only when its JSON value is an integer."""
    return (
        type(document.get("schema_version")) is int
        and document.get("schema_version") == expected
    )


__all__ = ["has_schema_version", "is_json_object"]

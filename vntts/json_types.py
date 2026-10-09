"""Small runtime-neutral predicates for JSON-shaped values."""

import json
from collections.abc import Mapping
from typing import TypeGuard


def decode_json(payload: str | bytes | bytearray) -> object:
    """Decode JSON, reporting unsupported nesting as a parse error."""
    try:
        return json.loads(payload)
    except RecursionError as error:
        raise json.JSONDecodeError(
            "JSON nesting exceeds the decoder limit", "", 0
        ) from error


def is_json_object(value: object) -> TypeGuard[dict[str, object]]:
    """Check that a value is a dictionary with string keys."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def has_schema_version(document: Mapping[str, object], expected: int) -> bool:
    """Match a schema version only when its JSON value is an integer."""
    return (
        type(document.get("schema_version")) is int
        and document.get("schema_version") == expected
    )


__all__ = ["decode_json", "has_schema_version", "is_json_object"]

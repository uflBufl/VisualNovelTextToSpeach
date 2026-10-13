"""Small runtime-neutral predicates for JSON-shaped values."""

import json
from collections.abc import Mapping
from typing import TypeGuard


def decode_json(payload: str | bytes | bytearray) -> object:
    """Decode JSON, reporting decoder limits as parse errors."""
    try:
        return json.loads(payload)
    except RecursionError as error:
        raise json.JSONDecodeError(
            "JSON nesting exceeds the decoder limit", "", 0
        ) from error
    except json.JSONDecodeError, UnicodeError:
        raise
    except ValueError as error:
        raise json.JSONDecodeError(str(error), "", 0) from error


def is_json_object(value: object) -> TypeGuard[dict[str, object]]:
    """Check that a value is a dictionary with string keys."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def is_utf8_text(value: object) -> TypeGuard[str]:
    """Check whether text can be persisted or hashed as UTF-8."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def has_schema_version(document: Mapping[str, object], expected: int) -> bool:
    """Match a schema version only when its JSON value is an integer."""
    return (
        type(document.get("schema_version")) is int
        and document.get("schema_version") == expected
    )


__all__ = ["decode_json", "has_schema_version", "is_json_object", "is_utf8_text"]

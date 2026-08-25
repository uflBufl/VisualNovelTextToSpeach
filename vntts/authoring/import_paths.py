"""Shared destination policy for non-destructive authoring imports."""

from vntts.settings import get_local_data_directory


def default_import_root():
    """Return the common immutable-import destination root."""
    return get_local_data_directory() / "authoring" / "legacy-imports"


__all__ = ["default_import_root"]

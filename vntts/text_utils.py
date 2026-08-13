"""Shared normalization for filesystem and identifier text."""

import re
import unicodedata


def slugify(value, *, fallback="item"):
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value).strip("-").casefold()
    return slug or fallback

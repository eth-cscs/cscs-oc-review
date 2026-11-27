"""Validate GitHub identifiers without coercing untrusted input."""

import re


_SHA = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)


def require_sha(value, *, field="GitHub object SHA"):
    """Return a full lowercase SHA; reject nonstrings and abbreviated object IDs."""
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(f"{field} must be a full lowercase commit/object SHA")
    return value


def require_id(value, *, field="GitHub ID"):
    """Return a positive integer ID; booleans are not GitHub identifiers."""
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value

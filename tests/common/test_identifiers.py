"""Reject identifiers that could select ambiguous Git objects or invalid GitHub IDs."""

import pytest

from common.identifiers import require_id, require_sha


@pytest.mark.parametrize("value", [
    None, 1, b"a" * 40, "A" * 40, "a" * 39, "a" * 41,
    "a" * 40 + "\n", "g" * 40, "é" * 40,
])
def test_sha_rejects_noncanonical_or_nonstring_identifiers(value):
    """Reject malformed object IDs before they can select a Git revision."""
    with pytest.raises(ValueError):
        require_sha(value)


@pytest.mark.parametrize("value", [False, True, 0, -1, 1.0, "1", None])
def test_id_rejects_nonpositive_noninteger_and_boolean_identifiers(value):
    """Reject values that must not select a numbered GitHub resource."""
    with pytest.raises(ValueError):
        require_id(value)

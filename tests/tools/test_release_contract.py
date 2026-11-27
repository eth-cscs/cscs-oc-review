"""Exercise the shared stable-version grammar and exact release-tag conversion."""

import pytest

from tools import release_contract as contract


INVALID_VERSIONS = (
    None, True, 123, "", "v1.2.3", "1.2.3-rc.1", "1.2.3+build",
    "01.2.3", "1.02.3", "1.2.03", "1.2", "١.2.3",
    "1.2.3\njobs: injected", "1.2.3;$(touch injected)", "1.2.3\n",
)


@pytest.mark.parametrize("version", ["0.0.0", "1.2.3", "12.30.40"])
def test_source_version_and_release_tag_round_trip(version):
    """Round-trip stable numeric versions without changing their components."""
    assert contract.version_tag(version) == "v" + version
    assert contract.version_from_tag("v" + version) == version


@pytest.mark.parametrize("version", INVALID_VERSIONS)
def test_invalid_source_versions_are_rejected(version):
    """Reject ambiguous, injected, and nonstable source-version spellings."""
    with pytest.raises(ValueError, match="MAJOR.MINOR.PATCH"):
        contract.version_tag(version)


@pytest.mark.parametrize("tag", (
    None, True, 123, "", "1.2.3", "V1.2.3", "vv1.2.3",
    *("v" + version for version in INVALID_VERSIONS if isinstance(version, str)),
))
def test_invalid_release_tags_are_rejected(tag):
    """Require the lowercase v prefix and the same strict stable-version grammar."""
    with pytest.raises(ValueError, match="MAJOR.MINOR.PATCH"):
        contract.version_from_tag(tag)

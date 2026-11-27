"""Dependency-free names and stable-version rules shared by release tooling."""

import re


REPOSITORY = "eth-cscs/cscs-oc-review"
REVIEW_WORKFLOW = REPOSITORY + "/.github/workflows/review.yml"
DEFAULT_CALLER_ASSET = "cscs-review.yml"
WEB_CALLER_ASSET = "cscs-review-web.yml"
CALLER_VARIANTS = ((DEFAULT_CALLER_ASSET, False), (WEB_CALLER_ASSET, True))
CALLER_PATH = ".github/workflows/cscs-review.yml"


def _require_version(version):
    """Validate the bare source version without constructing a tag."""
    if not isinstance(version, str) or not re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version, re.ASCII
    ):
        raise ValueError("release version must be MAJOR.MINOR.PATCH without a leading v")
    return version


def version_tag(version):
    """Return vMAJOR.MINOR.PATCH without accepting prefixes or semver suffixes."""
    return "v" + _require_version(version)


def version_from_tag(tag):
    """Return a stable source version from its exact vMAJOR.MINOR.PATCH tag."""
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError("release tag must be vMAJOR.MINOR.PATCH")
    return _require_version(tag[1:])

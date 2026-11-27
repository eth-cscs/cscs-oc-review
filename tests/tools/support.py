"""Shared request recording and errors for scenario-specific GitHub API fakes.

Keep release publication and post-release Git state in their own test modules;
only transport-independent bookkeeping and repository identity belong here.
"""

import copy
import urllib.error


REPOSITORY = "eth-cscs/cscs-oc-review"
BASE = f"/repos/{REPOSITORY}"


def http_error(code):
    """Construct the HTTP exception raised by the real GitHub transport."""
    return urllib.error.HTTPError("https://api.github.com/test", code, "test error", {}, None)


class RecordedGitHub:
    """Give each API fake independent repository metadata and request history."""

    def __init__(self):
        """Initialize isolated request history and canonical upstream metadata."""
        self.calls = []
        self.repository = {"full_name": REPOSITORY, "default_branch": "main"}

    def record(self, path, payload):
        """Snapshot a request before dispatch, including requests that will fail."""
        self.calls.append((path, copy.deepcopy(payload)))

    @property
    def writes(self):
        """Return attempted POSTs, including binary uploads and failed requests."""
        return [(path, payload) for path, payload in self.calls if payload is not None]

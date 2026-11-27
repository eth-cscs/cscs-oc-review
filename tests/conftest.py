"""Block production urllib traffic in every suite; HTTP tests opt into loopback."""

import urllib.request

import pytest


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    """Require injected APIs or a guarded loopback opener, never live GitHub calls."""
    def forbidden(*args, **kwargs):
        """Fail a test that tries to create a production urllib client."""
        pytest.fail("tests must not access the network")

    monkeypatch.setattr(urllib.request, "build_opener", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)

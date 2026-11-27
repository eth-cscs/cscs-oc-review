"""Resolve review-agent permissions with an explicitly supplied OpenCode CLI; no inference."""

import json
import os
from pathlib import Path
import subprocess

import pytest

from host import runtime_config
from tests.host.helpers import TRUSTED_ROOT


@pytest.fixture(scope="module")
def opencode_binary():
    """Skip unless the caller explicitly selects the CLI executable to check."""
    binary = os.environ.get("REVIEW_OPENCODE_BIN")
    if not binary:
        pytest.skip("set REVIEW_OPENCODE_BIN to the CLI version declared in image/Containerfile")
    return str(Path(binary).resolve(strict=True))


def effective_permissions(binary, directory, allow_web):
    """Load staged configuration in private directories without inherited credentials or project config."""
    home = directory / "home"
    home.mkdir()
    config_directory = home / "config/opencode"
    runtime_config.stage(TRUSTED_ROOT / "config", config_directory, allow_web=allow_web)
    config_path = config_directory / "opencode.json"
    config = json.loads(config_path.read_text())
    # Image-only policy files are irrelevant to permission merging and absent on the host.
    for agent in config["agents"].values():
        agent["system"] = "permission resolution fixture"
    config["instructions"] = []
    config_path.write_text(json.dumps(config))
    environment = {
        "PATH": os.environ.get("PATH", os.defpath), "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"), "XDG_STATE_HOME": str(home / "state"),
        "TMPDIR": str(directory), "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "CSCS_REVIEW_PROXY_URL": "http://127.0.0.1:9/v1", "CSCS_REVIEW_PROXY_TOKEN": "unused",
    }
    result = subprocess.run([binary, "debug", "agents", "--standalone"], cwd=home, env=environment,
                            check=True, capture_output=True, text=True, timeout=120)
    return {agent["id"]: agent["permissions"] for agent in json.loads(result.stdout)}


def last_effect(permissions, action):
    """Return the last global-resource rule matching the action, including wildcard actions."""
    effect = None
    for rule in permissions:
        if rule["action"] in (action, "*") and rule["resource"] == "*":
            effect = rule["effect"]
    return effect


@pytest.mark.parametrize("allow_web", [False, True])
def test_review_agents_keep_effective_web_and_edit_policy(opencode_binary, tmp_path, allow_web):
    """Require global edit/web denies to survive agent merging and keep finalization tool-free."""
    permissions = effective_permissions(opencode_binary, tmp_path, allow_web)
    for name in ("grumpy-reviewer", "review-finalizer"):
        assert last_effect(permissions[name], "edit") == "deny", name
        for action in ("websearch", "webfetch"):
            expected = "allow" if allow_web and name == "grumpy-reviewer" else "deny"
            assert last_effect(permissions[name], action) == expected, (name, action)
    for action in ("read", "shell", "execute"):
        assert last_effect(permissions["review-finalizer"], action) == "deny", action

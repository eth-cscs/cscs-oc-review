"""Check staged runtime files, web policy selection, and invalid workflow inputs."""

import json

import pytest

from host import runtime_config


@pytest.mark.parametrize("allow_web", [False, True])
def test_cli_stages_policy_without_changing_source(config_factory, tmp_path, allow_web):
    """Preserve trusted settings and source bytes while applying the requested web policy."""
    config = {"model": "fixture/reviewer", "permissions": [
        {"action": "edit", "resource": "*", "effect": "deny"}],
        "agents": {"fixture": {"steps": 2}}}
    source = config_factory(opencode=json.dumps(config).encode())
    before = {path.name: path.read_bytes() for path in source.iterdir()}
    target = tmp_path / "staged"
    runtime_config.main(["--source", str(source), "--target", str(target),
                         "--allow-web", "true" if allow_web else "false"])
    staged = json.loads((target / "opencode.json").read_text())
    permissions = staged.pop("permissions")
    assert staged == {key: value for key, value in config.items() if key != "permissions"}
    expected = config["permissions"]
    if not allow_web:
        expected = expected + [
            {"action": "websearch", "resource": "*", "effect": "deny"},
            {"action": "webfetch", "resource": "*", "effect": "deny"},
        ]
    assert permissions == expected
    for name in ("limits.json", "credential-proxy.json"):
        assert (target / name).read_bytes() == before[name]
    assert {path.name: path.read_bytes() for path in source.iterdir()} == before


@pytest.mark.parametrize("content", [b"{", b"[]", b"{}", b'{"permissions": {}}'])
def test_cli_rejects_invalid_json_or_permissions_shape(config_factory, tmp_path, content):
    """Reject invalid JSON or a missing permissions list before creating staged output."""
    source = config_factory(opencode=content)
    target = tmp_path / "staged"
    with pytest.raises(SystemExit) as error:
        runtime_config.main(["--source", str(source), "--target", str(target), "--allow-web", "false"])
    assert error.value.code == 1
    assert not target.exists()


@pytest.mark.parametrize("value", ["", "TRUE", "False", "1", "yes"])
def test_cli_rejects_non_literal_booleans(config_factory, tmp_path, value):
    """Reject ambiguous web settings instead of silently enabling or disabling tools."""
    source = config_factory(opencode=b'{"permissions": []}')
    target = tmp_path / "staged"
    with pytest.raises(SystemExit) as error:
        runtime_config.main(["--source", str(source), "--target", str(target), "--allow-web", value])
    assert error.value.code == 2
    assert not target.exists()


@pytest.mark.parametrize("value", ["false", None, 0])
def test_stage_requires_a_boolean(config_factory, tmp_path, value):
    """Prevent truthy strings or numeric flags from bypassing the web opt-in contract."""
    source = config_factory(opencode=b'{"permissions": []}')
    target = tmp_path / "staged"
    with pytest.raises(ValueError, match="must be a boolean"):
        runtime_config.stage(source, target, allow_web=value)
    assert not target.exists()

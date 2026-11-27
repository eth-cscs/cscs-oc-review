"""Controlled engine processes for runner lifecycle tests; no container daemon or inference."""

import json
import sys

import pytest


from host import run
from tests.host.helpers import commit, git


@pytest.fixture
def engine_factory(tmp_path):
    """Create an engine that tracks resources and emits private reviewer events."""
    def create(*, exit_code=0, hang=False):
        """Write a controllable engine executable and its run-resource ledger."""
        engine = tmp_path / "engine"
        state = tmp_path / "engine-state.json"
        state.write_text(json.dumps({"containers": {}, "network": False, "calls": []}))
        engine.write_text(
            f"#!{sys.executable}\n"
            f"STATE = {str(state)!r}\nEXIT_CODE = {exit_code!r}\nHANG = {hang!r}\n" + '''
import json, pathlib, signal, sys, time
path = pathlib.Path(STATE)
state = json.loads(path.read_text())
args = sys.argv[1:]
state['calls'].append(args)
def save():
    """Persist the fixture resource ledger after each simulated engine operation."""
    path.write_text(json.dumps(state))
if args[:2] == ['network', 'create']:
    state['network'] = True
elif args[:2] == ['network', 'ls']:
    if state['network']:
        print('network-id')
elif args[:2] == ['network', 'rm']:
    state['network'] = False
elif args[0] == 'run':
    name = args[args.index('--name') + 1]
    state['containers'][name] = True
    save()
    if '--detach' not in args:
        print(json.dumps({'type': 'reasoning', 'text': 'private model marker'}), flush=True)
        print(json.dumps({'type': 'private event marker'}), flush=True)
        print('private stderr credential marker', file=sys.stderr, flush=True)
        if HANG:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True:
                time.sleep(60)
        del state['containers'][name]
        save()
        sys.exit(EXIT_CODE)
elif args[0] == 'ps':
    print('\\n'.join(state['containers']))
elif args[0] == 'rm':
    for name in args[2:]:
        state['containers'].pop(name, None)
elif args[0] == 'inspect':
    print('true')
elif args[0] != 'exec':
    sys.exit(2)
save()
''', encoding="utf-8")
        engine.chmod(0o700)
        return engine, state
    return create


@pytest.fixture
def trusted_instructions(tmp_path):
    """Stage default-branch guidance outside the untrusted source checkout."""
    root = tmp_path / "default-branch"
    root.mkdir()
    instructions = root / "AGENTS.md"
    instructions.write_text("trusted repository instructions\n")
    return instructions


@pytest.fixture
def checkout(tmp_path):
    """Create a committed PR source tree for mutation and mount assertions."""
    checkout = tmp_path / "pr-source"
    checkout.mkdir()
    git(checkout, "init", "-b", "main")
    commit(checkout, "app.py", "important source\n")
    return checkout


@pytest.fixture
def runner():
    """Expose the trusted runner module for targeted lifecycle instrumentation."""
    return run


@pytest.fixture
def config_factory(tmp_path):
    """Write explicit trusted config snapshots with caller-selected limits and policy."""
    def create(*, name="trusted-config", opencode=b"{}\n", limits=None, policy=None):
        """Stage the selected policy, limits, and reviewer configuration externally."""
        config = tmp_path / name
        config.mkdir()
        (config / "opencode.json").write_bytes(opencode)
        (config / "limits.json").write_text(json.dumps(
            {"inspection_seconds": 10, "finalization_seconds": 5,
             "execution_overhead_seconds": 13, "termination_grace_seconds": 1}
            if limits is None else limits))
        (config / "credential-proxy.json").write_text(json.dumps(
            {"upstream": "https://api.inference.cscs.ch", "path": "/v1/chat/completions"}
            if policy is None else policy))
        return config
    return create


@pytest.fixture
def trusted_config(config_factory):
    """Provide the standard valid trusted configuration snapshot."""
    return config_factory()


@pytest.fixture
def review_input(tmp_path):
    """Write inert review context outside the source checkout."""
    context = tmp_path / "context.md"
    context.write_text("review input\n")
    return context


@pytest.fixture
def review_inputs(tmp_path, checkout, review_input, trusted_instructions, trusted_config):
    """Collect distinct trusted inputs and an external event-output destination."""
    return {
        "checkout": checkout,
        "context": review_input,
        "events": tmp_path / "events.jsonl",
        "instructions": trusted_instructions,
        "config_dir": trusted_config,
    }

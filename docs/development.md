# Development

## Source layout

| Directory | Responsibility |
| --- | --- |
| `common/` | Standard-library GitHub transport, REST pagination, and identifier validation |
| `host/` | Context preparation, container lifecycle, progress reporting, and final-response extraction |
| `tools/` | Caller generation and source/post-release preparation |
| `image/` | Container definition, phase runner, entrypoint, and baked reviewer policy |
| `config/` | Trusted model, execution-limit, and credential-proxy configuration |
| `tests/common/` | Shared transport and identifier contracts |
| `tests/host/` | Host behavior, runtime staging, and opt-in container/CLI checks; provider fixture in `fixtures/` |
| `tests/tools/` | Caller and release behavior; API recording and errors in `support.py` |
| `tests/runtime/` | Node.js phase-runner scenarios |

Shared code must not import host or release policy.
Release-domain constants belong in `tools/release_contract.py`.
Test fixtures remain under `tests/`; `tests/conftest.py` blocks production urllib clients for every Python suite.
HTTP transport tests explicitly use a guarded loopback fixture.

## Local checks

Run from the trusted repository root with Node.js 24 and uv available:

```bash
uv run --python 3.12 --with pytest --with-requirements tools/requirements.txt python -m pytest -q tests
node --test tests/runtime/test-run-review.mjs
bash -n image/entrypoint.sh
```

Ordinary tests use temporary repositories and controlled child processes, require no inference credentials, and skip container and CLI integration checks unless explicitly enabled.
CI also runs actionlint and parses CI YAML and embedded shell, including local composite actions.
Checks and both release workflows share [.github/actions/setup-validation-tools/](../.github/actions/setup-validation-tools/), which owns Python/uv setup and optional checksum-verified actionlint installation.
PyYAML is pinned in [tools/requirements.txt](../tools/requirements.txt) for these workflows and local commands; Dependabot checks the pin weekly.
Repeated action pins use file-local anchors where possible; Dependabot maintains literal cross-file pins.
Permissions, checkout inputs, and concurrency remain explicit rather than shared through anchors.

## OpenCode permission checks

Set `REVIEW_OPENCODE_BIN` to the absolute path of the OpenCode CLI version declared by `OPENCODE_VERSION` in [image/Containerfile](../image/Containerfile):

```bash
REVIEW_OPENCODE_BIN=/absolute/path/to/opencode uv run --python 3.12 --with pytest --with-requirements tools/requirements.txt python -m pytest -q tests/host/test_opencode_permissions.py
```

The tests resolve each review agent's effective permissions for both web settings without inference.
They use private home/configuration/data/cache directories and do not forward credentials or discover a CLI on `PATH`.
CI installs and checks the recipe's npm CLI version, then runs only this file; staging unit tests remain in the ordinary suite.
These checks do not verify the digest-pinned production image.

## Deterministic container tests

Use a Linux Docker daemon with working bridge networking:

```bash
REVIEW_CONTAINER_TEST_ENGINE=docker uv run --python 3.12 --with pytest --with-requirements tools/requirements.txt python -m pytest -q tests/host/test_proxy_integration.py
```

The suite reads both image references from the worker YAML and uses fake HTTPS inference and canary keys.
It needs neither a test repository service nor live inference.
Set `REVIEW_CONTAINER_TEST_IMAGE` to test a locally built reviewer candidate instead of the published pin.
A Podman adapter can exercise the containers on a private shared network, but does not verify Docker bridge behavior.

Changes to image inputs require a candidate rebuild before container testing.
See [image publication](releases.md#image-publication) before changing a production pin.

## Source conventions

Document every retained function, including test helpers and nested callbacks, with its purpose, inputs, or failure contract.
Use concise docstrings for Python and comments or JSDoc for JavaScript.
Write Markdown with one sentence per source line and no redundant or decorative prose.
Do not reformat Markdown under `image/`; it is baked into the runtime image.

Keep source data and modules at mode `0640` and directories at `0750` in the CSCS workspace.
Preserve ownership, group ACLs, and default ACLs; do not grant public access or follow symlinks during normalization.
Module-only Python CLIs are not executable and use `python -m` invocation.
The Containerfile sets runtime asset permissions independently of checkout modes.
Git records executable bits, not these local read/write modes or ACLs.
Exclude `.git`, agent state, caches, environments, private configuration, and generated runtime fixtures from source permission changes.

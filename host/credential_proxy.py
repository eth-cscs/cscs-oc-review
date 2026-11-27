"""Run a fixed-upstream credential proxy without sharing its secrets with the reviewer."""

import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib.parse import urlsplit

from host.container_policy import BASELINE_FLAGS


_PORT = 8080
_ALIAS = "credential-proxy"
_LABEL = "org.cscs.review.run"
# Per-command deadline, not a polling interval or a total cleanup budget.
_CONTAINER_COMMAND_TIMEOUT_SECONDS = 30


def load_policy(content):
    """Parse trusted policy; reject URLs that could change the upstream authority or route."""
    policy = json.loads(content)
    if not isinstance(policy, dict) or set(policy) != {"upstream", "path"}:
        raise ValueError("credential-proxy.json must define upstream and path")
    if not isinstance(policy["upstream"], str) or not isinstance(policy["path"], str):
        raise ValueError("credential proxy upstream and path must be strings")
    target = urlsplit(policy["upstream"])
    if (target.scheme != "https" or not target.hostname or target.username is not None
            or target.password is not None or target.path not in ("", "/")
            or target.query or target.fragment):
        raise ValueError("credential proxy upstream must be an HTTPS origin")
    # Resolve invalid ports before creating any resources.
    target.port
    path = policy["path"]
    if (not path.startswith("/v1/") or any(char in path for char in "?#{%\\")
            or any(char.isspace() for char in path) or "/../" in path or "/./" in path):
        raise ValueError("credential proxy path must be a literal /v1/ endpoint")
    return policy


def configuration(policy, token, key):
    """Render Caddy JSON; only the proxy receives this file, including the upstream key."""
    target = urlsplit(policy["upstream"])
    host = target.hostname
    authority = f"[{host}]" if ":" in host else host
    port = target.port or 443
    upstream_host = authority if port == 443 else f"{authority}:{port}"

    def response(status):
        """Build a fixed local rejection response without upstream access."""
        return {"handler": "static_response", "status_code": status}

    proxy = {
        "handler": "reverse_proxy",
        "upstreams": [{"dial": f"{authority}:{port}"}],
        "transport": {"protocol": "http", "tls": {"server_name": host}},
        "headers": {"request": {
            "set": {"Authorization": [f"Bearer {key}"], "Host": [upstream_host]},
            "delete": ["Proxy-Authorization", "X-Api-Key", "Api-Key", "Cookie"],
        }},
        # A fixed rewrite prevents encoded paths or query strings from selecting another endpoint.
        "rewrite": {"uri": policy["path"] + "?"},
        # Default SSE flushing preserves upstream cancellation when the client disconnects.
    }
    return {
        "admin": {"disabled": True, "config": {"persist": False}},
        # Neither payloads nor upstream error text enter CI logs.
        "logging": {"logs": {"default": {"writer": {"output": "discard"}}}},
        "apps": {"http": {"servers": {"proxy": {
            "listen": [f":{_PORT}"],
            "automatic_https": {"disable": True},
            "routes": [
                {"match": [{"path": ["/healthz"], "method": ["GET"]}],
                 "handle": [response(200)], "terminal": True},
                {"match": [{"header": {"Authorization": [f"Bearer {token}"]}}],
                 "handle": [{"handler": "subroute", "routes": [
                     {"match": [{"path": [policy["path"]], "method": ["POST"]}],
                      "handle": [proxy], "terminal": True},
                     {"handle": [response(404)]},
                 ]}], "terminal": True},
                {"handle": [response(401)], "terminal": True},
            ],
        }}}},
    }


class CredentialProxy:
    """Own run-labelled containers and network; leave reviewer internet access unrestricted."""

    def __init__(self, engine, image, run_id, directory, policy, key, deadline):
        """Bind run-scoped resources and credentials to the proxy startup deadline."""
        self.engine = engine
        self.image = image
        self.label = f"{_LABEL}={run_id}"
        self.network = f"opencode-review-{run_id}"
        self.name = f"{self.network}-proxy"
        self.token = secrets.token_urlsafe(32)
        self.url = f"http://{_ALIAS}:{_PORT}/v1"
        self.directory = Path(directory)
        self.policy = policy
        self.key = key
        self.deadline = deadline

    def _run(self, arguments, *, capture=False, cleanup=False):
        """Bound one engine command; cleanup retains a fresh deadline after startup expiry."""
        timeout = (_CONTAINER_COMMAND_TIMEOUT_SECONDS if cleanup else
                   min(_CONTAINER_COMMAND_TIMEOUT_SECONDS, self.deadline - time.monotonic()))
        if timeout <= 0:
            raise subprocess.TimeoutExpired(arguments, 0)
        return subprocess.run(
            [self.engine, *arguments], check=True, timeout=timeout,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True,
        )

    def start(self):
        """Start the proxy and wait for HTTP readiness; never inspect agent credentials."""
        path = self.directory / "proxy.json"
        with path.open("x", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(configuration(self.policy, self.token, self.key), output)
        uid, gid = os.getuid(), os.getgid()
        if uid == 0:
            uid, gid = 1000, 1000
            os.chown(path, uid, gid)
        self._run(["network", "create", "--label", self.label, self.network])
        self._run([
            "run", "--detach", "--name", self.name, *BASELINE_FLAGS,
            "--label", self.label, "--network", self.network, "--network-alias", _ALIAS,
            f"--user={uid}:{gid}", "--tmpfs=/tmp:rw,nosuid,nodev,mode=1777",
            "--env=XDG_CONFIG_HOME=/tmp/config", "--env=XDG_DATA_HOME=/tmp/data",
            "--volume", f"{path}:/etc/caddy/proxy.json:ro",
            self.image, "caddy", "run", "--config", "/etc/caddy/proxy.json",
        ])
        while True:
            try:
                self._run(["exec", self.name, "wget", "-q", "-O", "/dev/null",
                           f"http://127.0.0.1:{_PORT}/healthz"])
                return
            except subprocess.CalledProcessError:
                state = self._run(["inspect", "--format", "{{.State.Running}}", self.name], capture=True)
                if state.stdout.strip() != "true":
                    raise ValueError("credential proxy exited before readiness") from None
                if time.monotonic() >= self.deadline:
                    raise subprocess.TimeoutExpired("credential proxy readiness", 0) from None
                time.sleep(min(0.1, max(0, self.deadline - time.monotonic())))

    def cleanup(self):
        """Remove only this run's resources, continuing after each bounded cleanup failure."""
        complete = True
        for listing, removal in ((["ps", "--all", "--quiet"], ["rm", "--force"]),
                                 (["network", "ls", "--quiet"], ["network", "rm"])):
            try:
                result = self._run([*listing, "--filter", f"label={self.label}"],
                                   capture=True, cleanup=True)
                identifiers = result.stdout.split()
                if identifiers:
                    self._run([*removal, *identifiers], cleanup=True)
            except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
                complete = False
        return complete

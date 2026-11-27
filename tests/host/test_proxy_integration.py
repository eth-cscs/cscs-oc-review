"""Opt-in container tests using a fake HTTPS provider, never CSCS or real credentials."""

import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

import pytest

from host import credential_proxy, extract


ROOT = Path(__file__).resolve().parents[2]
ENGINE = os.environ.get("REVIEW_CONTAINER_TEST_ENGINE")
pytestmark = pytest.mark.skipif(not ENGINE, reason="set REVIEW_CONTAINER_TEST_ENGINE to run containers")
_CANARY = "fixture-upstream-canary-not-a-real-key"


@pytest.fixture
def runtime(tmp_path, monkeypatch, runner):
    """Use production proxy configuration with a test-only CA and HTTPS upstream."""
    import yaml

    engine = shutil.which(ENGINE)
    if not engine:
        pytest.fail("REVIEW_CONTAINER_TEST_ENGINE executable not found")
    worker = yaml.safe_load((ROOT / ".github/workflows/review-worker.yml").read_text())
    environment = worker["jobs"]["review"]["env"]
    proxy_image = environment["CREDENTIAL_PROXY_IMAGE"]
    image = os.environ.get("REVIEW_CONTAINER_TEST_IMAGE", environment["OPENCODE_REVIEW_IMAGE"])
    for selected in (proxy_image, image):
        available = subprocess.run([engine, "image", "inspect", selected], capture_output=True, timeout=30)
        if available.returncode:
            subprocess.run([engine, "pull", selected], check=True, capture_output=True, timeout=180)
    fixture = tmp_path / "provider"
    fixture.mkdir(mode=0o777)
    fixture.chmod(0o777)
    shutil.copyfile(Path(__file__).parent / "fixtures" / "proxy_provider.mjs", fixture / "server.mjs")
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=provider", "-addext", "subjectAltName=DNS:provider",
        "-keyout", str(fixture / "key.pem"), "-out", str(fixture / "cert.pem"),
    ], check=True, capture_output=True, timeout=30)
    for path in fixture.iterdir():
        path.chmod(0o444)
    control = credential_proxy.CredentialProxy._run
    launched = []

    def run(self, arguments, **kwargs):
        """Trust the fixture CA and start a keyed provider on the isolated per-run network."""
        if arguments[0] == "run" and self.image in arguments:
            index = arguments.index(self.image)
            arguments = [*arguments[:index], "--env=SSL_CERT_FILE=/etc/caddy/test-ca.pem",
                         "--volume", f"{fixture / 'cert.pem'}:/etc/caddy/test-ca.pem:ro",
                         *arguments[index:]]
        result = control(self, arguments, **kwargs)
        if arguments[:2] == ["network", "create"]:
            launched.append(self)
            # The fake provider shares only the per-run network, not reviewer inputs or proxy secrets.
            control(self, [
                "run", "--detach", "--name", self.network + "-provider", "--pull=never",
                "--label", self.label, "--network", self.network, "--network-alias", "provider",
                "--user=1000:1000", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--env", f"CANARY_KEY={_CANARY}",
                "--volume", f"{fixture}:/fixture:rw", "--entrypoint", "node", image,
                "/fixture/server.mjs",
            ])
            control(self, ["exec", self.network + "-provider", "node", "-e", """
const https = require('node:https');
const deadline = Date.now() + 5000;
// Retry provider readiness only until the fixture startup deadline.
function ready() {
  const request = https.get('https://127.0.0.1:9443/requests',
    {rejectUnauthorized:false}, /* Drain the readiness response. */ response => response.resume());
  // Retry connection failures while startup time remains.
  request.on('error', () => {
    if (Date.now() >= deadline) process.exit(1);
    else setTimeout(ready, 20);
  });
}
ready();
"""])
        return result

    monkeypatch.setattr(credential_proxy.CredentialProxy, "_run", run)
    policy = {"upstream": "https://provider:9443", "path": "/v1/chat/completions"}
    try:
        yield {"engine": engine, "image": image, "proxy_image": proxy_image,
               "fixture": fixture, "policy": policy, "proxies": launched}
    finally:
        for proxy in launched:
            assert proxy.cleanup(), "test resources could not be removed"


def client(runtime, proxy, script):
    """Run a Node client beside the proxy without mounting any proxy or host credentials."""
    result = subprocess.run([
        runtime["engine"], "run", "--rm", "--label", proxy.label, "--network", proxy.network,
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=1000:1000",
        "--entrypoint", "node", runtime["image"], "-e", script,
    ], check=True, capture_output=True, text=True, timeout=30)
    return json.loads(result.stdout)


def records(runtime):
    """Read the provider observation ledger, treating no received requests as an empty list."""
    path = runtime["fixture"] / "requests.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_proxy_authentication_routes_streaming_and_large_requests(runtime, tmp_path):
    """Assert proxy access controls, streaming, body integrity, and upstream cancellation independently."""

    proxy = credential_proxy.CredentialProxy(
        runtime["engine"], runtime["proxy_image"], uuid.uuid4().hex, tmp_path,
        runtime["policy"], _CANARY, time.monotonic() + 30,
    )
    try:
        proxy.start()
        # Administration must be disabled even inside the proxy's own network namespace.
        admin = subprocess.run([
            runtime["engine"], "exec", proxy.name, "wget", "-q", "-O", "/dev/null",
            "http://127.0.0.1:2019/config/",
        ], capture_output=True, timeout=5)
        assert admin.returncode != 0
        result = client(runtime, proxy, r'''
const http = require('node:http');
const https = require('node:https');
const crypto = require('node:crypto');
const token = TOKEN;
// Send a probe through the proxy and return its complete streamed response.
async function request(path, body, authorization = `Bearer ${token}`, method = 'POST') {
  // Resolve after the response ends; reject transport failures.
  return new Promise((resolve, reject) => {
    const req = http.request({host:'credential-proxy',port:8080,path,method,
      headers:{Authorization:authorization,'Content-Type':'application/json',
        'X-Api-Key':'untrusted-client-key','Proxy-Authorization':'untrusted-client-key',
        Host:'untrusted.invalid'}}, /* Collect the proxied status and body. */ response => {
      const chunks = [];
      let released = false;
      // Release the upstream only after observing its first streamed content.
      response.on('data', chunk => {
        chunks.push(chunk);
        if (!released && body?.includes('"probe":"stream"')
            && Buffer.concat(chunks).toString().includes('"content":"first"')) {
          released = true;
          // The provider cannot finish until the client receives its first content chunk.
          https.get('https://provider:9443/release', {rejectUnauthorized:false},
            /* Drain the provider release acknowledgment. */ response => response.resume()).on('error', reject);
        }
      });
      // Preserve the exact status and reassembled bytes for host assertions.
      response.on('end', () => resolve({status:response.statusCode, body:Buffer.concat(chunks).toString()}));
    });
    req.on('error', reject);
    // Fail stalled probes instead of leaving a client container running.
    req.setTimeout(5000, () => req.destroy(new Error('proxy response timed out')));
    req.end(body);
  });
}
// Exercise authentication, route, method, error, and large-stream boundaries.
(async () => {
  const bad = await request('/v1/chat/completions','{}','Bearer invalid');
  const route = await request('/debug','{}');
  const method = await request('/v1/chat/completions',null,`Bearer ${token}`,'GET');
  const error = await request('/v1/chat/completions',JSON.stringify({probe:'error'}));
  const body = JSON.stringify({probe:'stream',model:'fixture',padding:'x'.repeat(11*1024*1024)});
  const stream = await request('/v1/chat/completions?ignored=true',body);
  console.log(JSON.stringify({bad:bad.status,route:route.status,method:method.status,
    error,stream,digest:crypto.createHash('sha256').update(body).digest('hex'),bytes:Buffer.byteLength(body)}));
})().catch(/* Surface client failures as a nonzero fixture exit. */ error => { console.error(error); process.exit(1); });
'''.replace("TOKEN", json.dumps(proxy.token)))
        assert (result["bad"], result["route"], result["method"]) == (401, 404, 404)
        assert result["error"] == {"status": 429, "body": '{"error":"fixture rate limit"}'}
        assert result["stream"]["status"] == 200
        assert '"content":"first"' in result["stream"]["body"]
        assert '"content":"second"' in result["stream"]["body"]
        requests = records(runtime)
        assert len(requests) == 2  # Rejected client requests must never reach the keyed upstream.
        assert requests[-1]["digest"] == result["digest"]
        assert requests[-1]["bytes"] == result["bytes"]
        assert all(item["path"] == "/v1/chat/completions" and item["clientAuthRemoved"] for item in requests)
        aborted = client(runtime, proxy, r'''
const http = require('node:http');
const https = require('node:https');
const token = TOKEN;
// Read upstream observations directly, bypassing the proxy under test.
async function providerLog() {
  // Resolve only after the complete provider ledger has arrived.
  return new Promise((resolve,reject) => {
    // Collect the ledger response without interpreting individual records yet.
    https.get('https://provider:9443/requests', {rejectUnauthorized:false}, response => {
      let body = '';
      // Accumulate every ledger chunk before resolving the read.
      response.on('data', chunk => {body += chunk;});
      // Return the complete observation text for cancellation polling.
      response.on('end', () => resolve(body));
    }).on('error',reject);
  });
}
// Disconnect after the first chunk and require cancellation to reach the provider.
(async () => {
  // Resolve after deliberately closing the downstream stream.
  await new Promise((resolve,reject) => {
    const request = http.request({host:'credential-proxy',port:8080,path:'/v1/chat/completions',
      method:'POST',headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json'}},
      /* Wait for a response before registering the disconnect probe. */ response => response.once(
        'data', /* Cancel immediately after the first upstream chunk. */ () => {response.destroy();resolve();}));
    request.on('error',reject);
    // Bound the wait for the first chunk independently of upstream cancellation.
    request.setTimeout(5000, () => request.destroy(new Error('missing initial stream chunk')));
    request.end(JSON.stringify({probe:'stall'}));
  });
  const deadline = Date.now() + 3000;
  while (!(await providerLog()).includes('"aborted":true')) {
    if (Date.now() >= deadline) throw new Error('upstream was not cancelled');
    // Yield between ledger reads without changing the cancellation deadline.
    await new Promise(resolve => setTimeout(resolve,20));
  }
  console.log(JSON.stringify({cancelled:true}));
})().catch(/* Fail the fixture on transport or cancellation errors. */ error => {console.error(error);process.exit(1);});
'''.replace("TOKEN", json.dumps(proxy.token)))
        assert aborted["cancelled"]
    finally:
        assert proxy.cleanup()


def test_review_phases_use_proxy_without_exposing_credentials(
        runtime, runner, review_inputs, config_factory, monkeypatch):
    """Exercise both review phases while proving credential separation and readonly source access."""
    checkout = review_inputs["checkout"]
    config = config_factory(
        name="config", opencode=(ROOT / "config/opencode.json").read_bytes(),
        policy=runtime["policy"],
        limits={"inspection_seconds": 20, "finalization_seconds": 10,
                "execution_overhead_seconds": 15, "termination_grace_seconds": 1},
    )
    context = review_inputs["context"]
    context.write_text("Review the single app.py file in this fixture PR.\n")
    events = review_inputs["events"]
    monkeypatch.setenv("CSCS_INFERENCE_KEY", _CANARY)
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-host-github-token")
    runner.run_review(engine=runtime["engine"], image=runtime["image"],
                      proxy_image=runtime["proxy_image"],
                      **(review_inputs | {"config_dir": config}))
    with events.open() as lines:
        review = extract.extract_review(lines)
    assert "Credential proxy fixture review." in review
    assert _CANARY not in events.read_text()
    requests = records(runtime)
    final = [request for request in requests if request.get("final")]
    assert final and final[-1]["max_tokens"] == 32768
    assert final[-1]["reasoning_effort"] == "low"
    inspection = [request for request in requests if not request.get("final")]
    assert inspection[0]["max_tokens"] == 16384 and inspection[0]["reasoning_effort"] == "high"
    messages = json.dumps(final[-1]["messages"])
    # Parse the probe result from tool output; the source stays immutable even with a shell.
    match = re.search(r"PROXY_BOUNDARY:([A-Za-z0-9+/]+=*)", messages)
    assert match, messages
    boundary = json.loads(base64.b64decode(match[1]))
    assert boundary == {"realKeyPresent": False, "githubTokenPresent": False,
                        "proxyTokenPresent": True, "proxyConfigVisible": False,
                        "sourceWritable": False, "uid": 1000}
    assert (checkout / "app.py").read_text() == "important source\n"
    proxy = runtime["proxies"][0]
    for arguments in (["ps", "--all", "--quiet"], ["network", "ls", "--quiet"]):
        remaining = subprocess.run([runtime["engine"], *arguments, "--filter", f"label={proxy.label}"],
                                   check=True, capture_output=True, text=True, timeout=10)
        assert not remaining.stdout.strip()

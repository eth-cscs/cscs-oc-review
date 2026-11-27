"""Exercise API authentication and public asset redirects over loopback only."""

import http.client
import http.server
import json
import socket
import threading
import urllib.request

import pytest

from common import github
from tools import post_release
from tests.tools.support import BASE, REPOSITORY


BUILD_OPENER = urllib.request.build_opener
TOKEN = "test-only-not-a-real-github-token"
ASSET_PATH = BASE + "/releases/123/assets?name=cscs-review.yml"
JSON_PATH = BASE + "/releases"
RELEASE_TAG = "v1.2.3"
DOWNLOAD_PATH = f"/{REPOSITORY}/releases/download/{RELEASE_TAG}/cscs-review-web.yml"
REDIRECT_STATUSES = (301, 302, 303, 307, 308)


@pytest.fixture
def loopback_github(monkeypatch):
    """Keep urllib serialization/redirect handling; route HTTPS sockets to localhost."""
    received = []
    responses = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        """Serve recorded requests without exposing the tests to external hosts."""
        def handle_request(self):
            """Record request bytes and headers, then send the configured fixture response."""
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append({
                "method": self.command, "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            })
            status, headers, response = responses.get(
                self.path, (200, {}, b'{"accepted": true}'))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        do_GET = handle_request
        do_POST = handle_request

        def log_message(self, format, *args):
            """Suppress server logs because assertions inspect the captured requests."""
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    address = server.server_address
    connect = socket.create_connection

    def local_connection(target, *args, **kwargs):
        """Reject socket connections outside the fixture's loopback address."""
        if target != address:
            pytest.fail(f"external connection forbidden: {target!r}")
        return connect(target, *args, **kwargs)

    class LoopbackHTTPS(urllib.request.HTTPSHandler):
        """Retain logical HTTPS URLs while replacing their sockets with local HTTP."""
        def https_open(self, request):
            """Route the request locally without changing its logical Host header."""
            return self.do_open(
                lambda host, timeout: http.client.HTTPConnection(*address, timeout=timeout),
                request,
            )

    def opener(*handlers):
        """Disable ambient proxies and retain the selected redirect handlers."""
        return BUILD_OPENER(urllib.request.ProxyHandler({}), LoopbackHTTPS(), *handlers)

    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setattr(socket, "create_connection", local_connection)
    monkeypatch.setattr(github.urllib.request, "build_opener", opener)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield received, responses
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("body", [b"", bytes(range(256)) + b"\r\ncaller: \x00\xff\n"])
def test_binary_upload_reaches_upload_origin_without_byte_corruption(loopback_github, body):
    """Upload empty and binary assets unchanged to the authenticated upload origin."""
    received, _ = loopback_github

    assert github.upload_asset(ASSET_PATH, body) == {"accepted": True}

    assert len(received) == 1
    request = received[0]
    assert request["method"] == "POST"
    assert request["path"] == ASSET_PATH
    assert request["body"] == body
    assert request["headers"]["host"] == "uploads.github.com"
    assert request["headers"]["authorization"] == "Bearer " + TOKEN
    assert request["headers"]["content-type"] == "application/octet-stream"
    assert int(request["headers"]["content-length"]) == len(body)


@pytest.mark.parametrize("payload", [None, {"draft": True, "body": "Release notes: café\n"}])
def test_json_requests_remain_on_api_origin(loopback_github, payload):
    """Keep JSON reads and writes on the API origin with the expected body."""
    received, _ = loopback_github

    assert github.github_api(JSON_PATH, payload) == {"accepted": True}

    assert len(received) == 1
    request = received[0]
    assert request["method"] == ("GET" if payload is None else "POST")
    assert request["path"] == JSON_PATH
    assert request["headers"]["host"] == "api.github.com"
    assert request["headers"]["authorization"] == "Bearer " + TOKEN
    assert request["headers"]["content-type"] == "application/json"
    if payload is None:
        assert request["body"] == b""
    else:
        assert json.loads(request["body"]) == payload


@pytest.mark.parametrize("status", REDIRECT_STATUSES)
@pytest.mark.parametrize("destination", [
    "https://api.github.com/redirect-target",
    "https://credential-thief.invalid/redirect-target",
])
@pytest.mark.parametrize("payload", [{"draft": True}, b"caller bytes\x00\xff"])
def test_authenticated_redirects_never_reach_the_destination(loopback_github, status, destination, payload):
    """Reject every authenticated redirect before a second request can carry the token."""
    received, responses = loopback_github
    path = ASSET_PATH if isinstance(payload, bytes) else JSON_PATH
    responses[path] = (status, {"Location": destination}, b"")

    send = github.upload_asset if isinstance(payload, bytes) else github.github_api
    with pytest.raises(ValueError, match="redirects are not allowed"):
        send(path, payload)

    # A redirect would reach this same loopback server even for the hostile host.
    # Observing only the initial request proves no credentials reached the target.
    assert len(received) == 1
    assert received[0]["path"] == path
    assert received[0]["headers"]["authorization"] == "Bearer " + TOKEN


@pytest.mark.parametrize("status", REDIRECT_STATUSES)
def test_public_asset_redirects_preserve_bytes_without_forwarding_credentials(loopback_github, status):
    """Follow public HTTPS redirects without adding credentials or changing asset bytes."""
    received, responses = loopback_github
    path = DOWNLOAD_PATH
    body = b"# published caller\r\nallow-web: true\n"
    responses[path] = (status, {"Location": "https://release-assets.githubusercontent.com/caller"}, b"")
    responses["/caller"] = (200, {}, body)

    assert post_release.download_caller(RELEASE_TAG) == body

    assert [item["headers"]["host"] for item in received] == [
        "github.com", "release-assets.githubusercontent.com",
    ]
    assert [item["path"] for item in received] == [path, "/caller"]
    assert all("authorization" not in item["headers"] for item in received)
    assert all(item["body"] == b"" for item in received)


@pytest.mark.parametrize("destination", [
    "http://release-assets.githubusercontent.com/caller",
    "https://username:password@release-assets.githubusercontent.com/caller",
])
def test_public_asset_redirect_cannot_downgrade_https_or_include_credentials(
        loopback_github, destination):
    """Reject public redirects that expose credentials or downgrade transport security."""
    received, responses = loopback_github
    path = DOWNLOAD_PATH
    responses[path] = (302, {"Location": destination}, b"")

    with pytest.raises(ValueError, match="HTTPS without credentials"):
        post_release.download_caller(RELEASE_TAG)

    assert len(received) == 1
    assert "authorization" not in received[0]["headers"]


def test_oversized_public_asset_fails_instead_of_truncating_the_caller(loopback_github):
    """Reject oversized downloads rather than returning a truncated caller."""
    _, responses = loopback_github
    path = DOWNLOAD_PATH
    responses[path] = (200, {}, b"x" * (post_release.MAX_CALLER_BYTES + 1))

    with pytest.raises(ValueError, match="size limit"):
        post_release.download_caller(RELEASE_TAG)


@pytest.mark.parametrize("send,payload", [
    (github.github_api, {"draft": True}),
    (github.upload_asset, b"caller bytes"),
])
@pytest.mark.parametrize("path", ["https://other.invalid/path", "//other.invalid/path", "", None])
def test_authenticated_transport_rejects_nonrelative_paths(loopback_github, send, payload, path):
    """Reject absolute or ambiguous request paths before sending authentication."""
    received, _ = loopback_github
    with pytest.raises(ValueError):
        send(path, payload)
    assert received == []


def test_json_api_cannot_implicitly_route_byte_payloads_to_uploads(loopback_github):
    """Require the explicit upload operation for binary payloads."""
    received, _ = loopback_github
    with pytest.raises(TypeError):
        github.github_api(JSON_PATH, b"not JSON")
    assert received == []


def test_api_reads_the_current_token_for_each_request(loopback_github, monkeypatch):
    """Use a rotated GH_TOKEN on the next request instead of retaining the old value."""
    received, _ = loopback_github
    github.github_api(JSON_PATH)
    monkeypatch.setenv("GH_TOKEN", "rotated-test-token")
    github.github_api(JSON_PATH)
    assert [item["headers"]["authorization"] for item in received] == [
        "Bearer " + TOKEN, "Bearer rotated-test-token",
    ]

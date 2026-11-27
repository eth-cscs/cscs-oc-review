"""Authenticated GitHub transport and REST pagination for trusted host helpers.

Authenticated requests reject redirects and use fixed origins.
Public release downloads have a separate, unauthenticated transport.
"""

import json
import os
from urllib.parse import urlencode
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Reject redirects before a GitHub token can reach another request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Reject the redirect before urllib can forward authentication headers."""
        raise ValueError("GitHub API redirects are not allowed")


def _request(path, data, *, origin, content_type, method):
    """Send one authenticated request to a fixed origin and decode its JSON response."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ValueError("GitHub API path must start with a single slash")
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise ValueError("GH_TOKEN is required")
    request = urllib.request.Request(
        origin + path,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "cscs-oc-review",
                 "Content-Type": content_type},
        data=data, method=method,
    )
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
        return json.load(response)


def github_api(path, payload=None):
    """GET or POST JSON to api.github.com; read GH_TOKEN for each request."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    return _request(path, data, origin="https://api.github.com", content_type="application/json",
                    method="GET" if payload is None else "POST")


def upload_asset(path, content):
    """POST unchanged asset bytes to uploads.github.com and decode its JSON response."""
    if not isinstance(content, bytes):
        raise ValueError("release asset content must be bytes")
    return _request(path, content, origin="https://uploads.github.com",
                    content_type="application/octet-stream", method="POST")


def rest_pages(api, path, *, params=None, items_key=None):
    """Yield validated REST pages; callers retain filtering and completeness policy.

    items_key selects an endpoint's list envelope, such as workflow_runs.
    Identity and cursor checks remain with the domain-specific history readers.
    """
    page = 1
    while True:
        query = urlencode({**(params or {}), "per_page": 100, "page": page})
        response = api(f"{path}?{query}")
        if items_key is not None:
            if not isinstance(response, dict) or items_key not in response:
                raise ValueError("malformed GitHub REST page envelope")
            items = response[items_key]
        else:
            items = response
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError("malformed GitHub REST page")
        yield items
        if len(items) < 100:
            return
        page += 1

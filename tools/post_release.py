"""Install a published caller and advance VERSION through one maintenance PR.

Read release assets as data, never as executable code. Git Data API writes create
one commit on a new branch; the default branch and published release are unchanged.
Reruns consult PR history before checking branch existence or current VERSION.
"""

import argparse
import base64
import binascii
import urllib.error
import urllib.parse
import urllib.request

import yaml

from common.github import github_api, rest_pages
from common.identifiers import require_id, require_sha

from .caller import workflow
from . import release_contract as contract


MAX_CALLER_BYTES = 64 * 1024


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    """Permit public download redirects only to HTTPS URLs without user info."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Reject redirects that could expose credentials or downgrade transport."""
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or not target.hostname or target.username or target.password:
            raise ValueError("caller download redirects must use HTTPS without credentials")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_caller(tag):
    """Download the exact release's public web caller without sending GH_TOKEN.

    GitHub redirects public assets to its storage service. Keep this client
    separate from authenticated API calls and bound the downloaded input size.
    """
    contract.version_from_tag(tag)
    url = (f"https://github.com/{contract.REPOSITORY}/releases/download/{tag}/"
           f"{contract.WEB_CALLER_ASSET}")
    request = urllib.request.Request(url, headers={"User-Agent": "cscs-oc-review-release"})
    with urllib.request.build_opener(HTTPSRedirect()).open(request, timeout=60) as response:
        content = response.read(MAX_CALLER_BYTES + 1)
    if len(content) > MAX_CALLER_BYTES:
        raise ValueError("release caller exceeds the download size limit")
    return content


def _optional(api, path):
    """Treat only HTTP 404 as absence; authorization and service errors propagate."""
    try:
        return api(path)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return None


def _file(api, base, path, revision, *, optional=False):
    """Read a regular file at an immutable revision, preserving its decoded bytes."""
    query = urllib.parse.urlencode({"ref": revision})
    url = f"{base}/contents/{urllib.parse.quote(path, safe='/')}?{query}"
    data = _optional(api, url) if optional else api(url)
    if data is None and optional:
        return None
    if data.get("type") != "file" or data.get("encoding") != "base64":
        raise ValueError(f"{path} must be a regular, base64-encoded repository file")
    try:
        # GitHub wraps base64 contents across lines; whitespace is not file data.
        return base64.b64decode("".join(data["content"].split()), validate=True)
    except (binascii.Error, AttributeError, KeyError, TypeError) as error:
        raise ValueError(f"{path} has invalid GitHub file contents") from error


def _tag_commit(api, base, tag):
    """Peel lightweight or annotated tags; reject malformed objects and cycles."""
    obj = api(f"{base}/git/ref/tags/{tag}")["object"]
    seen = set()
    while True:
        revision = require_sha(obj["sha"])
        if obj.get("type") == "commit":
            return revision
        if obj.get("type") != "tag" or revision in seen:
            raise ValueError("release tag must resolve to a commit without cycles")
        seen.add(revision)
        obj = api(f"{base}/git/tags/{revision}")["object"]


def _ancestor(api, base, older, newer):
    """Require equal commits or forward ancestry; reject downgrades and divergence."""
    if older != newer:
        comparison = api(f"{base}/compare/{older}...{newer}")
        if comparison.get("status") not in {"ahead", "identical"}:
            raise ValueError("release ancestry conflict; refusing a caller downgrade or divergent source")


def _caller_revision(content):
    """Validate the canonical web caller without regenerating the installed bytes.

    Compare parsed structure with the shared generator to avoid a second caller
    schema. Comments and formatting survive; custom jobs, inputs, permissions,
    or secrets require manual resolution instead of being silently overwritten.
    """
    if not isinstance(content, bytes) or len(content) > MAX_CALLER_BYTES:
        raise ValueError("release caller must be bounded UTF-8 YAML bytes")
    try:
        document = yaml.safe_load(content.decode("utf-8"))
        reference = document["jobs"]["review"]["uses"]
        upstream, separator, revision = reference.rpartition("@")
        if upstream != contract.REVIEW_WORKFLOW or not separator:
            raise ValueError("caller must pin the upstream review workflow to a full SHA")
        require_sha(revision, field="caller revision")
        if document != yaml.safe_load(workflow(revision, allow_web=True)):
            raise ValueError("caller must be the standard web-enabled workflow without custom inputs")
        return revision
    except (UnicodeError, yaml.YAMLError, AttributeError, KeyError, TypeError) as error:
        raise ValueError("caller must be valid UTF-8 YAML with a pinned web-enabled review job") from error


def _previous_pr(api, base, branch):
    """Find all matching PR history, including merged PRs whose branch was deleted."""
    owner = contract.REPOSITORY.split("/")[0]
    matches = []
    for pulls in rest_pages(
        api, f"{base}/pulls", params={"state": "all", "head": f"{owner}:{branch}"},
    ):
        matches.extend(pr for pr in pulls if pr.get("head", {}).get("ref") == branch
                       and (pr["head"].get("repo") or {}).get("full_name") == contract.REPOSITORY)
    if len(matches) > 1:
        raise ValueError("multiple maintenance PRs exist for this release; inspect their history")
    return matches[0] if matches else None


def _result(state, pr, tag):
    """Build a summary link from the upstream PR number, not an API-supplied URL."""
    number = require_id(pr.get("number"), field="maintenance PR number")
    return {"state": state, "url": f"https://github.com/{contract.REPOSITORY}/pull/{number}", "tag": tag}


def _resume_branch(api, base, revision, parent, updates, changed):
    """Accept an orphan only when its sole commit is the exact planned update."""
    commit = api(f"{base}/git/commits/{revision}")
    if [item["sha"] for item in commit["parents"]] != [parent]:
        raise ValueError("existing maintenance branch has a different parent; inspect it manually")
    comparison = api(f"{base}/compare/{parent}...{revision}")
    files = comparison.get("files", [])
    if (comparison.get("status") != "ahead"
            or {item["filename"] for item in files} != changed
            or any(item["status"] not in {"added", "modified"} for item in files)):
        raise ValueError("existing maintenance branch contains unexpected changes")
    if any(_file(api, base, path, revision) != content for path, content in updates.items()):
        raise ValueError("existing maintenance branch does not match the planned caller and version")


def _create_branch(api, base, branch, parent, updates, tag):
    """Commit the planned files on a new ref; never update or force-push a ref.

    Only VERSION differs when the installed caller already matches the asset.
    """
    tree = require_sha(api(f"{base}/git/commits/{parent}")["tree"]["sha"])
    entries = []
    for path, content in updates.items():
        blob = api(f"{base}/git/blobs", {
            "encoding": "base64", "content": base64.b64encode(content).decode("ascii"),
        })
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": require_sha(blob["sha"])})
    tree = require_sha(api(f"{base}/git/trees", {"base_tree": tree, "tree": entries})["sha"])
    # GitHub uses the authenticated App bot as author and committer by default.
    commit = api(f"{base}/git/commits", {
        "message": f"Prepare development after {tag}", "tree": tree, "parents": [parent],
    })
    api(f"{base}/git/refs", {"ref": "refs/heads/" + branch, "sha": require_sha(commit["sha"])})


def prepare(repository, release_id, *, api=github_api, download=download_caller):
    """Return a created/open/merged/closed maintenance PR result for one release.

    Require a published upstream stable release, matching source versions, forward
    ancestry, and a canonical web caller. Resolve all conflicts before creating a
    branch. Failures can leave an orphan branch, which a rerun checks before reuse.
    """
    if repository != contract.REPOSITORY:
        raise ValueError("post-release preparation requires upstream")
    require_id(release_id, field="release ID")
    base = f"/repos/{repository}"
    source = api(base)
    if source.get("full_name") != contract.REPOSITORY or not source.get("default_branch"):
        raise ValueError("post-release preparation requires the upstream default branch")
    default_branch = source["default_branch"]
    release = api(f"{base}/releases/{release_id}")
    if (release.get("id") != release_id or release.get("draft") is not False
            or release.get("prerelease") is not False or not release.get("published_at")):
        raise ValueError("post-release preparation requires a published stable release")
    tag = release["tag_name"]
    version = contract.version_from_tag(tag)
    branch = f"maintenance/post-release-{tag}"
    previous = _previous_pr(api, base, branch)
    if previous is not None:
        if previous.get("state") == "open":
            if previous.get("base", {}).get("ref") != default_branch:
                raise ValueError("existing maintenance PR does not target the default branch")
            state = "open"
        elif previous.get("state") == "closed":
            state = "merged" if previous.get("merged_at") else "closed"
        else:
            raise ValueError("maintenance PR has an invalid state")
        return _result(state, previous, tag)

    released = _tag_commit(api, base, tag)
    default_ref = f"{base}/git/ref/heads/{urllib.parse.quote(default_branch, safe='/')}"
    head = require_sha(api(default_ref)["object"]["sha"])
    _ancestor(api, base, released, head)
    released_version = _file(api, base, "VERSION", released)
    if released_version.decode("utf-8").strip() != version:
        raise ValueError("release tag does not match the released VERSION")
    current_version = released_version if head == released else _file(api, base, "VERSION", head)
    if current_version.decode("utf-8").strip() != version:
        raise ValueError("default-branch VERSION differs from the release; resolve the version conflict")
    major, minor, patch = map(int, version.split("."))
    next_version = f"{major}.{minor}.{patch + 1}"
    caller = download(tag)
    if _caller_revision(caller) != released:
        raise ValueError("release caller does not pin the released commit")
    existing = _file(api, base, contract.CALLER_PATH, head, optional=True)
    if existing is not None:
        _ancestor(api, base, _caller_revision(existing), released)
    updates = {"VERSION": (next_version + "\n").encode("utf-8"), contract.CALLER_PATH: caller}
    changed = {path for path, content in updates.items()
               if content != (current_version if path == "VERSION" else existing)}
    branch_ref = f"{base}/git/ref/heads/{branch}"
    orphan = _optional(api, branch_ref)
    if orphan is not None:
        _resume_branch(api, base, require_sha(orphan["object"]["sha"]), head, updates, changed)
    # Recheck before writing in case another release or maintainer advanced the default branch.
    if require_sha(api(default_ref)["object"]["sha"]) != head:
        raise ValueError("default branch advanced during preparation; rerun against its current state")
    if orphan is None:
        _create_branch(api, base, branch, head, updates, tag)
    pr = api(f"{base}/pulls", {
        "head": branch, "base": default_branch,
        "title": f"Prepare development after {tag}",
        "body": (
            f"## Post-release maintenance\n\n"
            f"- Release: [{tag}](https://github.com/{repository}/releases/tag/{tag}).\n"
            f"- Install the unchanged `{contract.WEB_CALLER_ASSET}` asset as `{contract.CALLER_PATH}`.\n"
            f"- Pin the reviewer to `{released}` with web search and fetch enabled.\n"
            f"- Advance `VERSION` from `{version}` to `{next_version}` for the next development cycle.\n\n"
            "Web queries and fetched URLs can disclose repository details outside CSCS.\n"
            "The caller forwards only `CSCS_INFERENCE_KEY`, not the maintenance App credentials.\n"
            "Review CI and merge normally; this PR does not change the published release.\n"
        ),
    })
    return _result("created", pr, tag)


def main(argv=None):
    """Print a Markdown job summary; report conflicts and API failures as CLI errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-id", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.repository, args.release_id)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"post-release preparation: {error}\n")
    print(f"## Post-release maintenance for {result['tag']}\n")
    print(f"PR state: **{result['state']}**. [Maintenance PR]({result['url']}).")
    if result["state"] == "closed":
        print("The PR was closed without merging. No changes were made; resolve it manually.")
    elif result["state"] != "created":
        print("No changes were made to the existing PR or branch.")


if __name__ == "__main__":
    main()

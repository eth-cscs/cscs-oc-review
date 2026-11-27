"""Prepare release callers and attach them to an unpublished GitHub release.

Run prepare from the trusted source checkout, lint both generated callers, then
run create on the same output directory. API calls use GH_TOKEN; source CI is
queried, not rerun. Failed creation or upload leaves unpublished state for inspection.
"""

import argparse
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse

import yaml

from common.github import github_api, rest_pages, upload_asset
from common.identifiers import require_id, require_sha

from .caller import workflow
from . import release_contract as contract


CHECKS_WORKFLOW = ".github/workflows/checks.yml"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
REVIEWER_IMAGE_REPOSITORY = "ghcr.io/eth-cscs/cscs-oc-review/opencode-review"
PROXY_IMAGE_REPOSITORY = "docker.io/library/caddy"


def require_image_pin(image, repository):
    """Require an untagged SHA-256 pin from the exact approved image repository."""
    if not isinstance(image, str):
        raise ValueError(f"expected a digest-pinned image from {repository}")
    name, separator, digest = image.partition("@")
    if name != repository or not separator or not DIGEST.fullmatch(digest):
        raise ValueError(f"expected a digest-pinned image from {repository}")


def require_image_pins(reviewer_image, proxy_image):
    """Validate the independently pinned reviewer/proxy pair at each release phase."""
    require_image_pin(reviewer_image, REVIEWER_IMAGE_REPOSITORY)
    require_image_pin(proxy_image, PROXY_IMAGE_REPOSITORY)


def require_unused_version(repository, tag, api=github_api):
    """Raise ValueError if a tag or release, including a draft, already uses tag.

    Scan all release pages. Only HTTP 404 means the tag is absent; other API
    failures propagate rather than allowing release creation.
    """
    try:
        api(f"/repos/{repository}/git/ref/tags/{tag}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    else:
        raise ValueError(f"tag {tag} already exists; existing versions are never overwritten")
    # The tag lookup alone cannot find an unpublished draft with no Git tag yet.
    for releases in rest_pages(api, f"/repos/{repository}/releases"):
        if any(release["tag_name"] == tag for release in releases):
            raise ValueError(f"release {tag} already exists, including drafts")


def require_successful_checks(repository, revision, branch, api=github_api):
    """Require the latest matching checks.yml run to succeed; return its URL.

    Match the exact SHA, push event, branch, workflow path, and run/head
    repositories across all pages. Rank by run ID and attempt, not success,
    so an older success cannot hide a newer failure or pending run.
    Raise ValueError if no eligible run exists or the latest is not successful.
    """
    candidates = []
    for runs in rest_pages(
        api, f"/repos/{repository}/actions/workflows/checks.yml/runs",
        params={"head_sha": revision, "event": "push", "branch": branch},
        items_key="workflow_runs",
    ):
        candidates.extend(run for run in runs
                          if run.get("head_sha") == revision and run.get("head_branch") == branch
                          and run.get("event") == "push" and run.get("path") == CHECKS_WORKFLOW
                          and run.get("repository", {}).get("full_name") == repository
                          and run.get("head_repository", {}).get("full_name") == repository)
    if not candidates:
        raise ValueError(f"no checks.yml push run found for {revision} on {branch}; run upstream CI first")
    # A rerun keeps its run ID; its attempt number determines recency within that run.
    latest = max(candidates, key=lambda run: (run["id"], run.get("run_attempt", 1)))
    if latest.get("status") != "completed" or latest.get("conclusion") != "success":
        raise ValueError(f"latest checks.yml run for {revision} is not successful "
                         f"({latest.get('status')}/{latest.get('conclusion')}); finish or fix CI first")
    return f"https://github.com/{repository}/actions/runs/{latest['id']}"


def prepare(repository, revision, ref, output, api=github_api):
    """Validate release eligibility; write both callers and metadata.json.

    Run from the trusted checkout of revision. Require the upstream default
    branch, a stable VERSION, successful existing CI, an unused version, and a
    digest-pinned reviewer and credential proxy images from approved repositories.
    output must not exist. Return release metadata.
    This phase reads GitHub state but does not create tags or releases.
    """
    if repository != contract.REPOSITORY:
        raise ValueError("release preparation requires the upstream repository")
    require_sha(revision, field="release revision")
    version = Path("VERSION").read_text(encoding="utf-8").strip()
    tag = contract.version_tag(version)
    source = api(f"/repos/{repository}")
    branch = source["default_branch"]
    if source["full_name"] != repository or ref != "refs/heads/" + branch:
        raise ValueError("release preparation is restricted to the upstream default branch")
    checks_url = require_successful_checks(repository, revision, branch, api)
    require_unused_version(repository, tag, api)
    worker = yaml.safe_load(Path(".github/workflows/review-worker.yml").read_text(encoding="utf-8"))
    environment = worker["jobs"]["review"]["env"]
    reviewer_image = environment.get("OPENCODE_REVIEW_IMAGE")
    proxy_image = environment.get("CREDENTIAL_PROXY_IMAGE")
    require_image_pins(reviewer_image, proxy_image)
    metadata = {"repository": repository, "revision": revision, "version": version,
                "tag": tag, "reviewer_image": reviewer_image, "proxy_image": proxy_image,
                "checks_url": checks_url}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    for filename, allow_web in contract.CALLER_VARIANTS:
        (output / filename).write_text(
            workflow(revision, allow_web=allow_web, release_version=version), encoding="utf-8")
    (output / "metadata.json").write_text(json.dumps(metadata) + "\n", encoding="utf-8")
    return metadata


def release_notes(metadata, changes):
    """Return installation links, provenance, web disclosure, image pins, and changes.

    Download links identify this release, not latest. Caller YAML is distributed
    only as assets so there is no second copy to maintain in the notes.
    """
    repository, revision = metadata["repository"], metadata["revision"]
    downloads = f"https://github.com/{repository}/releases/download/{metadata['tag']}"
    standard, web = (f"{downloads}/{filename}" for filename, _ in contract.CALLER_VARIANTS)
    return (
        "## Installation\n\n"
        f"Source version: **{metadata['tag']}** · "
        f"[Commit `{revision}`](https://github.com/{repository}/commit/{revision}) · "
        f"[Successful checks]({metadata['checks_url']})\n\n"
        "Download **one** caller from this release:\n\n"
        f"- [{contract.DEFAULT_CALLER_ASSET}]({standard}): web tools disabled; recommended default.\n"
        f"- [{contract.WEB_CALLER_ASSET}]({web}): web tools enabled; explicit opt-in.\n\n"
        f"Save either download as `{contract.CALLER_PATH}` in the subscriber repository "
        "and merge it into its default branch.\n"
        "Provide the `CSCS_INFERENCE_KEY` Actions secret, then post `/cscs-review` on a PR.\n"
        "The requester needs repository write access.\n"
        "Installed callers remain SHA-pinned until you update them.\n\n"
        "> [!WARNING]\n"
        "> Web search queries and fetched URLs can disclose repository information outside CSCS.\n"
        "> Enable the web-enabled caller only if your repository's data policy permits it.\n"
        "> Disabling web tools is not a firewall for the agent's shell.\n\n"
        "## Runtime images\n\n"
        "Reviewer:\n\n```text\n" + metadata["reviewer_image"] + "\n```\n\n"
        "Credential proxy:\n\n```text\n" + metadata["proxy_image"] + "\n```\n\n"
        "Both images are independently pinned by digest.\n"
        "This source release does not rebuild them.\n"
        "Only the credential proxy container receives the real inference key; "
        "the reviewer receives a per-run proxy URL and token.\n"
        "This credential boundary does not restrict the reviewer's network egress.\n\n"
        "## Changes\n\n" + changes + "\n"
    )


def create_draft(output, api=github_api, *, upload=upload_asset):
    """Create a tag and draft, upload both callers, then return the draft URL.

    Caller files must have passed actionlint; read their exact bytes without
    regeneration. Recheck version availability and generate the changelog,
    then reserve the captured SHA and upload assets to the new draft.
    Never publish, overwrite, retry, or delete remote state. A failed upload
    leaves an incomplete draft for inspection and does not return a success URL.
    """
    output = Path(output)
    metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    repository, revision, tag = metadata["repository"], metadata["revision"], metadata["tag"]
    if repository != contract.REPOSITORY or tag != contract.version_tag(metadata["version"]):
        raise ValueError("invalid prepared release metadata")
    require_sha(revision, field="prepared release revision")
    require_image_pins(metadata.get("reviewer_image"), metadata.get("proxy_image"))
    assets = {filename: (output / filename).read_bytes() for filename, _ in contract.CALLER_VARIANTS}
    require_unused_version(repository, tag, api)
    generated = api(f"/repos/{repository}/releases/generate-notes",
                    {"tag_name": tag, "target_commitish": revision})
    notes = release_notes(metadata, generated["body"])
    (output / "release-notes.md").write_text(notes, encoding="utf-8")
    # Reserve the exact tag target now, rather than following later default-branch movement.
    api(f"/repos/{repository}/git/refs", {"ref": "refs/tags/" + tag, "sha": revision})
    release = api(f"/repos/{repository}/releases",
                  {"tag_name": tag, "target_commitish": revision, "name": tag,
                   "body": notes, "draft": True, "prerelease": False})
    release_id = require_id(release.get("id"), field="draft release ID")
    if release.get("draft") is not True:
        raise ValueError("GitHub did not return an identified draft release")
    for filename, content in assets.items():
        query = urllib.parse.urlencode({"name": filename})
        asset = upload(f"/repos/{repository}/releases/{release_id}/assets?{query}", content)
        if (asset.get("state") != "uploaded" or asset.get("name") != filename
                or asset.get("size") != len(content)):
            raise ValueError(f"release asset {filename} was not uploaded completely")
    return release["html_url"]


def main(argv=None):
    """CLI: prepare prints a status line; create prints the draft URL for the job summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("prepare", help="validate existing CI and generate both caller variants")
    check.add_argument("--repository", required=True)
    check.add_argument("--revision", required=True)
    check.add_argument("--ref", required=True)
    check.add_argument("--output", required=True, type=Path)
    create = commands.add_parser("create", help="create a draft and upload both actionlint-validated callers")
    create.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            metadata = prepare(args.repository, args.revision, args.ref, args.output)
            print(f"Prepared {metadata['tag']} at {metadata['revision']}; existing checks passed")
        else:
            print(create_draft(args.output))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"release preparation: {error}\n")


if __name__ == "__main__":
    main()

# Releases

## Image publication

The runtime image contains OpenCode, the phase runner, and the reviewer policy.
The OpenCode version is set by `OPENCODE_VERSION` in [image/Containerfile](../image/Containerfile).
The GHCR package is `ghcr.io/eth-cscs/cscs-oc-review/opencode-review`; tags use `opencode-<opencode-version>-amd64-v<image-revision>`.
Configuration and consumer instructions are mounted separately.
Runtime or policy changes require a new image build and digest update; configuration-only changes do not.

1. Set `OPENCODE_REVIEW_IMAGE` in the [image pipeline](../.cscs/workflows/build-opencode-review-image.yaml) to an unused tag.
   The `if-not-exists` policy reuses existing tags instead of rebuilding them.
2. Post this PR comment:

   ```text
   cscs-ci run build-reviewer-image
   ```

3. Wait for the report job to verify the AMD64 architecture and anonymous registry access.
4. Set `jobs.review.env.OPENCODE_REVIEW_IMAGE` in [review-worker.yml](../.github/workflows/review-worker.yml) to the reported `OPENCODE_REVIEW_IMAGE_PIN`.
   A local ARM candidate or unpublished image is not a production replacement.

The worker separately pins the official multiarchitecture Caddy image in `CREDENTIAL_PROXY_IMAGE`.
Source-release preparation validates both digest pins.

## Source releases

[tools/release.py](../tools/release.py) validates [VERSION](../VERSION) and prefixes it with `v` for the release tag.
Versions use `MAJOR.MINOR.PATCH` without leading zeros, prerelease suffixes, or build metadata.
Source, image, and OpenCode versions are independent.
Use patch versions for compatible fixes, minor versions for compatible features, and major versions for incompatible changes.
Before the first major release, incompatible changes increment the minor version.
Preparation rejects existing tags and releases, but does not enforce increasing version numbers.

1. Merge reviewed changes and any version update into the upstream default branch.
   Keep that branch protected and immutable releases enabled; preparation does not configure or verify those settings.
2. Wait for the latest matching `checks.yml` **push** run to succeed for that exact SHA on the upstream default branch.
   PR runs, fork runs, and other branches or commits do not qualify.
3. Run **Actions → Prepare release → Run workflow** on that branch.
   The workflow reads `VERSION`, rejects reused versions including drafts, generates web-disabled and web-enabled callers, and actionlints both without repeating source suites.
4. The workflow generates change notes, reserves the version tag at the dispatch SHA, creates a draft, and uploads the validated files unchanged as `cscs-review.yml` and `cscs-review-web.yml`.
   Notes include version-specific download links, source provenance, the qualifying checks link, and both image pins.
5. Inspect the notes and both assets, then manually publish the draft and mark it as latest.

Preparation uses repository-scoped `GITHUB_TOKEN` with `contents: write` and `actions: read`, without a PAT or inference key.
It does not build images, commit generated files, or publish the release.
Creation or upload failures can leave an incomplete draft, tag, or assets; there is no automatic retry or cleanup.
Inspect the result and remove only unpublished leftovers before retrying; never publish an incomplete draft.
Published immutable releases prevent replacing assets or reusing their versions.

## Generate a caller locally

Run from the trusted checkout root with uv available:
Replace `FULL_COMMIT_SHA` with a lowercase, 40-character SHA that contains the reusable workflows and is available in the public shared repository:

```bash
uv run --python 3.12 --with-requirements tools/requirements.txt python -m tools.caller --revision FULL_COMMIT_SHA --release-version "$(cat VERSION)" > /tmp/cscs-review.yml
```

The generator prints YAML without modifying repositories, calling GitHub, or installing workflows.
It rejects branches, tags, abbreviated SHAs, and GitHub expressions in literal instruction paths.
Add `--allow-web` or `--review-instructions-path docs/review.md` for optional inputs.
`--release-version` adds only a comment; the SHA selects executable code.

## Post-release maintenance

The upstream-only [Prepare post-release](../.github/workflows/prepare-post-release.yml) workflow handles `release.published` for stable releases in `eth-cscs/cscs-oc-review`.
It loads trusted helpers from the current default branch and performs no inference.
[tools/post_release.py](../tools/post_release.py) opens `maintenance/post-release-vX.Y.Z` against that branch and can change only:

- `VERSION`: advance from published `X.Y.Z` to `X.Y.(Z+1)` for development.
- `.github/workflows/cscs-review.yml`: install the published `cscs-review-web.yml` asset unchanged, pinning its released full SHA and enabling web tools.

An already identical caller needs no new commit entry.
Subscriber installation and upgrades remain manual; see the [consumer guide](../README.md#consumer-setup).
A maintainer reviews and merges the maintenance PR after normal CI.
There is no automatic merge or modification of the published release.

The workflow uses variable `POST_RELEASE_APP_CLIENT_ID` and secret `POST_RELEASE_APP_PRIVATE_KEY` to mint a repository-scoped App installation token.
The token is revoked after the job, and the App bot creates the maintenance commit and PR.
App credentials are never forwarded to review jobs or inference.

### Validation and reruns

Preparation rejects tag/source-version mismatches, released commits outside default-branch ancestry, caller downgrades, and missing or invalid assets.
A new PR requires the current default-branch `VERSION` to equal the released version.
The asset must match the released SHA and web-enabled caller setting; it is validated as data and never executed.
Authenticated GitHub requests reject redirects; public release downloads carry no credentials and accept HTTPS redirects only.

Reruns find PR history by the deterministic branch even after branch deletion.
An existing open PR is reused without overwriting contents, a merged PR is a no-op, and a closed unmerged PR is reported without reopening it.
An orphan branch is recoverable only when it has one commit based on the current default-branch head and changes that match the planned caller/version update.
Conflicting contents or incompatible default-branch state stop preparation instead of being overwritten.
Resolve the reported conflict or setup issue, then rerun the failed workflow.

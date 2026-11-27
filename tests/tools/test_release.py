"""Test release guards and draft creation with isolated files and no network.

The API fixture supplies paginated responses and failures; no test mutates GitHub.
"""

import copy
import json
import urllib.error
import urllib.parse

import pytest
import yaml

from tools import release

from .support import REPOSITORY, RecordedGitHub, http_error


REVISION = "a" * 40
REVIEWER_IMAGE_REPOSITORY = "ghcr.io/eth-cscs/cscs-oc-review/opencode-review"
PROXY_IMAGE_REPOSITORY = "docker.io/library/caddy"
REVIEWER_IMAGE = REVIEWER_IMAGE_REPOSITORY + "@sha256:" + "b" * 64
PROXY_IMAGE = PROXY_IMAGE_REPOSITORY + "@sha256:" + "c" * 64
CALLER_FILES = ("cscs-review.yml", "cscs-review-web.yml")


def successful_run(**changes):
    """Build an eligible CI run; override fields to exercise rejection paths."""
    run = {
        "id": 10, "run_attempt": 1, "head_sha": REVISION, "head_branch": "main",
        "event": "push", "path": ".github/workflows/checks.yml",
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
        "status": "completed", "conclusion": "success",
    }
    run.update(changes)
    return run


class GitHub(RecordedGitHub):
    """Record requests and serve configured run/release pages or injected failures."""

    def __init__(self):
        """Start with eligible CI, an unused version, and successful upload responses."""
        super().__init__()
        self.runs = {1: [successful_run()]}
        self.releases = {1: []}
        self.tag_error = http_error(404)
        self.create_error = None
        self.draft = {
            "id": 123, "draft": True,
            "html_url": "https://github.com/eth-cscs/cscs-oc-review/releases/123",
        }
        self.upload_errors = {}
        self.upload_responses = {}

    @property
    def uploads(self):
        """Return attempted binary uploads in request order."""
        return [(path, payload) for path, payload in self.calls if isinstance(payload, bytes)]

    def __call__(self, path, payload=None):
        """Record and dispatch fixture API calls; validate workflow query parameters."""
        self.record(path, payload)
        base = f"/repos/{REPOSITORY}"
        if path == base and payload is None:
            return copy.deepcopy(self.repository)
        if path.startswith(base + "/actions/workflows/checks.yml/runs?") and payload is None:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            assert query == {
                "head_sha": [REVISION], "event": ["push"], "branch": ["main"],
                "per_page": ["100"], "page": query["page"],
            }
            return {"workflow_runs": copy.deepcopy(self.runs[int(query["page"][0])])}
        if path.startswith(base + "/git/ref/tags/") and payload is None:
            if self.tag_error is not None:
                raise self.tag_error
            return {"object": {"sha": REVISION}}
        if path.startswith(base + "/releases?") and payload is None:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            assert query["per_page"] == ["100"]
            return copy.deepcopy(self.releases[int(query["page"][0])])
        if path == base + "/releases/generate-notes" and payload is not None:
            return {"body": "Generated changes from merged pull requests."}
        if path == base + "/git/refs" and payload is not None:
            return {"ref": payload["ref"], "object": {"sha": payload["sha"]}}
        if path == base + "/releases" and payload is not None:
            if self.create_error is not None:
                raise self.create_error
            return copy.deepcopy(self.draft)
        if path.startswith(base + f"/releases/{self.draft.get('id')}/assets?") and isinstance(payload, bytes):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            assert set(query) == {"name"}
            filename, = query["name"]
            assert filename in CALLER_FILES
            if filename in self.upload_errors:
                raise self.upload_errors[filename]
            return copy.deepcopy(self.upload_responses.get(filename, {
                "state": "uploaded", "name": filename, "size": len(payload),
            }))
        pytest.fail(f"unexpected API request: {path}, {payload!r}")


@pytest.fixture
def source(tmp_path, monkeypatch):
    """Provide a temporary source checkout with VERSION and both worker image pins."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    worker = tmp_path / ".github" / "workflows" / "review-worker.yml"
    worker.parent.mkdir(parents=True)
    worker.write_text(yaml.safe_dump({
        "jobs": {"review": {"env": {
            "OPENCODE_REVIEW_IMAGE": REVIEWER_IMAGE,
            "CREDENTIAL_PROXY_IMAGE": PROXY_IMAGE,
        }}},
    }), encoding="utf-8")
    return tmp_path


@pytest.fixture
def prepared_release(source):
    """Prepare one eligible release while keeping each scenario's API independent."""
    api = GitHub()
    output = source / "prepared"
    release.prepare(REPOSITORY, REVISION, "refs/heads/main", output, api)
    return api, output


@pytest.mark.parametrize("endpoint", ["checks", "releases"])
def test_malformed_later_page_prevents_preparation_and_all_writes(source, endpoint):
    """Abort each eligibility scan on a malformed later page before producing files."""
    api = GitHub()
    if endpoint == "checks":
        api.runs = {1: [successful_run(id=n) for n in range(1, 101)], 2: None}
    else:
        api.releases = {
            1: [{"tag_name": f"v0.0.{n}"} for n in range(100)], 2: None,
        }
    output = source / "prepared"
    with pytest.raises(ValueError, match="malformed GitHub REST page"):
        release.prepare(REPOSITORY, REVISION, "refs/heads/main", output, api)
    assert api.calls[-1][0].endswith("page=2")
    assert not api.writes
    assert not output.exists()


def test_malformed_release_page_prevents_creation_before_all_writes(prepared_release):
    """Recheck every release page before generating notes or reserving a tag."""
    api, output = prepared_release
    api.calls.clear()
    api.releases = {
        1: [{"tag_name": f"v0.0.{n}"} for n in range(100)], 2: None,
    }
    with pytest.raises(ValueError, match="malformed GitHub REST page"):
        release.create_draft(output, api, upload=api)
    assert api.calls[-1][0].endswith("page=2")
    assert not api.writes
    assert not (output / "release-notes.md").exists()


@pytest.mark.parametrize("changes", [
    {"head_sha": "c" * 40}, {"head_branch": "other"}, {"event": "pull_request"},
    {"path": ".github/workflows/other.yml"},
    {"repository": {"full_name": "contributor/fork"}},
    {"head_repository": {"full_name": "contributor/fork"}},
])
def test_success_from_an_untrusted_run_cannot_authorize_release(changes):
    """Prevent fork, wrong-event, and wrong-workflow successes from authorizing a release."""
    api = GitHub()
    api.runs[1] = [successful_run(**changes)]
    with pytest.raises(ValueError, match="no checks.yml push run"):
        release.require_successful_checks(REPOSITORY, REVISION, "main", api)
    assert not api.writes


@pytest.mark.parametrize("runs", [
    [], [successful_run(status="in_progress", conclusion=None)],
    [successful_run(conclusion="failure")],
])
def test_missing_pending_or_failed_checks_block_release(runs):
    """Require a completed successful upstream check rather than merely an existing run."""
    api = GitHub()
    api.runs[1] = runs
    with pytest.raises(ValueError):
        release.require_successful_checks(REPOSITORY, REVISION, "main", api)


@pytest.mark.parametrize("newer", [
    successful_run(id=11, conclusion="failure"),
    successful_run(id=10, run_attempt=2, conclusion="failure"),
])
def test_newer_failure_supersedes_older_success_even_when_listed_first(newer):
    """Rank run IDs and rerun attempts before evaluating the latest conclusion."""
    api = GitHub()
    api.runs[1] = [newer, successful_run()]
    with pytest.raises(ValueError, match="latest checks.yml run"):
        release.require_successful_checks(REPOSITORY, REVISION, "main", api)


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_checks_scan_later_pages_before_selecting_latest(conclusion):
    """Use later-page runs when choosing the latest eligible CI result."""
    api = GitHub()
    api.runs = {
        1: [successful_run(id=number) for number in range(1, 101)],
        2: [successful_run(id=101, conclusion=conclusion)],
    }
    if conclusion == "success":
        url = release.require_successful_checks(REPOSITORY, REVISION, "main", api)
        assert url == f"https://github.com/{REPOSITORY}/actions/runs/101"
    else:
        with pytest.raises(ValueError, match="latest checks.yml run"):
            release.require_successful_checks(REPOSITORY, REVISION, "main", api)
    assert len(api.calls) == 2


def test_existing_tag_blocks_version_without_scanning_releases():
    """Stop immediately when an immutable release tag already exists."""
    api = GitHub()
    api.tag_error = None
    with pytest.raises(ValueError, match="tag v1.2.3 already exists"):
        release.require_unused_version(REPOSITORY, "v1.2.3", api)
    assert len(api.calls) == 1
    assert not api.writes


def test_existing_draft_on_later_release_page_blocks_version():
    """Reject unpublished drafts even when they appear beyond the first page."""
    api = GitHub()
    api.releases = {
        1: [{"tag_name": f"v0.0.{number}", "draft": False} for number in range(100)],
        2: [{"tag_name": "v1.2.3", "draft": True}],
    }
    with pytest.raises(ValueError, match="including drafts"):
        release.require_unused_version(REPOSITORY, "v1.2.3", api)
    assert api.calls[-1][0].endswith("page=2")
    assert not api.writes


def test_only_missing_tag_and_no_release_allow_a_new_version():
    """Allow a fresh version only after checking both tags and all releases."""
    api = GitHub()
    release.require_unused_version(REPOSITORY, "v1.2.3", api)
    assert [path for path, _ in api.calls] == [
        f"/repos/{REPOSITORY}/git/ref/tags/v1.2.3",
        f"/repos/{REPOSITORY}/releases?per_page=100&page=1",
    ]
    assert not api.writes


@pytest.mark.parametrize("error", [http_error(403), http_error(500), urllib.error.URLError("offline")])
def test_tag_lookup_errors_fail_closed(error):
    """Propagate tag-lookup failures instead of treating them as absence."""
    api = GitHub()
    api.tag_error = error
    with pytest.raises(type(error)) as caught:
        release.require_unused_version(REPOSITORY, "v1.2.3", api)
    assert caught.value is error
    assert len(api.calls) == 1
    assert not api.writes


def test_invalid_source_version_cannot_prepare_or_access_remote(source):
    """Validate source version syntax before any remote lookup or generated output."""
    (source / "VERSION").write_text("1.2.3\nmalicious: true\n", encoding="utf-8")
    api = GitHub()
    with pytest.raises(ValueError, match="MAJOR.MINOR.PATCH"):
        release.prepare(REPOSITORY, REVISION, "refs/heads/main", source / "prepared", api)
    assert not api.calls
    assert not (source / "prepared").exists()


@pytest.mark.parametrize("repository,ref", [
    ("contributor/fork", "refs/heads/main"),
    (REPOSITORY, "refs/heads/feature"),
    (REPOSITORY, "refs/tags/v1.2.3"),
])
def test_wrong_repository_or_ref_cannot_prepare(source, repository, ref):
    """Restrict preparation to the canonical repository's default-branch ref."""
    api = GitHub()
    with pytest.raises(ValueError, match="upstream"):
        release.prepare(repository, REVISION, ref, source / "prepared", api)
    assert not api.writes
    assert not (source / "prepared").exists()


def test_api_repository_identity_must_match_upstream(source):
    """Reject API repository metadata that disagrees with the upstream identity."""
    api = GitHub()
    api.repository["full_name"] = "contributor/fork"
    with pytest.raises(ValueError, match="upstream default branch"):
        release.prepare(REPOSITORY, REVISION, "refs/heads/main", source / "prepared", api)
    assert not api.writes
    assert not (source / "prepared").exists()


@pytest.mark.parametrize("existing", ["tag", "draft"])
def test_existing_version_prevents_preparation_and_all_remote_writes(source, existing):
    """Leave no prepared output or remote writes when a tag or draft claims the version."""
    api = GitHub()
    if existing == "tag":
        api.tag_error = None
    else:
        api.releases[1] = [{"tag_name": "v1.2.3", "draft": True}]
    with pytest.raises(ValueError, match="already exists"):
        release.prepare(REPOSITORY, REVISION, "refs/heads/main", source / "prepared", api)
    assert not api.writes
    assert not (source / "prepared").exists()


@pytest.mark.parametrize("repository", [REVIEWER_IMAGE_REPOSITORY, PROXY_IMAGE_REPOSITORY])
@pytest.mark.parametrize("suffix", [
    "", ":latest", ":v1@sha256:" + "b" * 64,
    "@sha256:" + "b" * 63, "@sha256:" + "b" * 65,
    "@sha256:" + "B" * 64, "@sha512:" + "b" * 64,
    "@sha256:" + "b" * 64 + "\n", "@sha256:" + "b" * 64 + "@extra",
])
def test_image_pin_requires_exact_digest_not_tag_or_malformed_digest(repository, suffix):
    """Accept only an untagged approved repository with a complete lowercase SHA-256 pin."""
    with pytest.raises(ValueError, match="digest-pinned image"):
        release.require_image_pin(repository + suffix, repository)


@pytest.mark.parametrize("variable,pin", [
    ("OPENCODE_REVIEW_IMAGE", None),
    ("CREDENTIAL_PROXY_IMAGE", None),
    ("OPENCODE_REVIEW_IMAGE", True),
    ("CREDENTIAL_PROXY_IMAGE", True),
    ("OPENCODE_REVIEW_IMAGE", PROXY_IMAGE),
    ("CREDENTIAL_PROXY_IMAGE", REVIEWER_IMAGE),
    ("OPENCODE_REVIEW_IMAGE", "ghcr.io/untrusted/reviewer@sha256:" + "b" * 64),
    ("CREDENTIAL_PROXY_IMAGE", "ghcr.io/library/caddy@sha256:" + "b" * 64),
    ("CREDENTIAL_PROXY_IMAGE", "docker.io/untrusted/caddy@sha256:" + "b" * 64),
    ("CREDENTIAL_PROXY_IMAGE", "docker.io/library/caddy/extra@sha256:" + "b" * 64),
])
def test_prepare_requires_both_pins_from_their_approved_repositories(source, variable, pin):
    """Validate each image independently before writing either caller variant."""
    worker = source / ".github/workflows/review-worker.yml"
    data = yaml.safe_load(worker.read_text(encoding="utf-8"))
    environment = data["jobs"]["review"]["env"]
    if pin is None:
        environment.pop(variable)
    else:
        environment[variable] = pin
    worker.write_text(yaml.safe_dump(data), encoding="utf-8")
    api = GitHub()

    with pytest.raises(ValueError, match="digest-pinned image"):
        release.prepare(REPOSITORY, REVISION, "refs/heads/main", source / "prepared", api)

    assert not api.writes
    assert not (source / "prepared").exists()


@pytest.mark.parametrize("version", ["0.0.0", "12.30.40"])
def test_prepare_then_create_pins_draft_and_both_caller_variants(source, version):
    """Publish both captured caller byte streams with exact source and image provenance."""
    (source / "VERSION").write_text(version + "\n", encoding="utf-8")
    api = GitHub()
    output = source / "prepared"
    metadata = release.prepare(REPOSITORY, REVISION, "refs/heads/main", output, api)
    assert metadata["version"] == version
    assert metadata["tag"] == "v" + version
    assert metadata["revision"] == REVISION
    assert metadata["reviewer_image"] == REVIEWER_IMAGE
    assert metadata["proxy_image"] == PROXY_IMAGE
    assert json.loads((output / "metadata.json").read_text(encoding="utf-8")) == metadata
    assert not api.writes
    prepared = {filename: (output / filename).read_bytes() for filename in CALLER_FILES}
    # Creation must use the already validated files, not regenerate from source.
    (source / "VERSION").write_text("99.99.99\n", encoding="utf-8")

    url = release.create_draft(output, api, upload=api)
    assert url == "https://github.com/eth-cscs/cscs-oc-review/releases/123"
    generated, reserved, created, *uploaded = api.writes
    assert generated == (f"/repos/{REPOSITORY}/releases/generate-notes", {
        "tag_name": "v" + version, "target_commitish": REVISION,
    })
    assert reserved == (f"/repos/{REPOSITORY}/git/refs", {
        "ref": "refs/tags/v" + version, "sha": REVISION,
    })
    path, payload = created
    assert path == f"/repos/{REPOSITORY}/releases"
    assert payload["draft"] is True
    assert payload["prerelease"] is False
    assert payload["target_commitish"] == REVISION
    assert payload["tag_name"] == payload["name"] == "v" + version
    notes = payload["body"]
    assert (output / "release-notes.md").read_text(encoding="utf-8") == notes
    assert REVIEWER_IMAGE in notes
    assert PROXY_IMAGE in notes
    assert metadata["checks_url"] in notes
    assert metadata["tag"] in notes
    assert f"https://github.com/{REPOSITORY}/commit/{REVISION}" in notes
    assert ".github/workflows/cscs-review.yml" in notes
    assert "CSCS_INFERENCE_KEY" in notes
    assert "Generated changes from merged pull requests." in notes
    assert len(uploaded) == 2
    callers = []
    for upload, filename, allow_web in zip(uploaded, CALLER_FILES, (False, True)):
        path, content = upload
        assert path == f"/repos/{REPOSITORY}/releases/123/assets?name={filename}"
        assert isinstance(content, bytes)
        assert content == prepared[filename]
        assert content == (output / filename).read_bytes()
        assert f"https://github.com/{REPOSITORY}/releases/download/v{version}/{filename}" in notes
        assert content.decode("utf-8") not in notes
        caller = yaml.safe_load(content)
        callers.append(caller)
        job = caller["jobs"]["review"]
        assert job["uses"] == f"{REPOSITORY}/.github/workflows/review.yml@{REVISION}"
        assert job.get("with", {}).get("allow-web", False) is allow_web
        assert job["secrets"] == {"CSCS_INFERENCE_KEY": "${{ secrets.CSCS_INFERENCE_KEY }}"}
        assert caller["permissions"] == {"contents": "read", "pull-requests": "write"}
    assert "```yaml" not in notes
    assert "/releases/latest/" not in notes
    assert "with" not in callers[0]["jobs"]["review"]
    assert callers[1]["jobs"]["review"].pop("with") == {"allow-web": True}
    assert callers[0] == callers[1]


@pytest.mark.parametrize("field", ["reviewer_image", "proxy_image"])
@pytest.mark.parametrize("pin", [
    None, True, "not-an-image", "docker.io/untrusted/image@sha256:" + "b" * 64,
])
def test_create_revalidates_both_prepared_pins_before_remote_access(prepared_release, field, pin):
    """Reject tampered prepared image metadata before any remote call or generated notes."""
    api, output = prepared_release
    path = output / "metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if pin is None:
        metadata.pop(field)
    else:
        metadata[field] = pin
    path.write_text(json.dumps(metadata), encoding="utf-8")
    api.calls.clear()

    with pytest.raises(ValueError, match="digest-pinned image"):
        release.create_draft(output, api, upload=api)

    assert not api.calls
    assert not (output / "release-notes.md").exists()


def test_draft_appearing_after_preparation_prevents_creation(prepared_release):
    """Recheck draft availability at creation time to catch post-preparation races."""
    api, output = prepared_release
    api.releases[1] = [{"tag_name": "v1.2.3", "draft": True}]
    with pytest.raises(ValueError, match="including drafts"):
        release.create_draft(output, api, upload=api)
    assert not api.writes
    assert not (output / "release-notes.md").exists()


def test_draft_creation_failure_after_tag_reservation_propagates_without_cleanup_or_retry(prepared_release):
    """Leave the reserved tag and generated notes intact when draft creation fails."""
    api, output = prepared_release
    error = http_error(503)
    api.create_error = error
    with pytest.raises(urllib.error.HTTPError) as caught:
        release.create_draft(output, api, upload=api)
    assert caught.value is error
    assert [path for path, _ in api.writes] == [
        f"/repos/{REPOSITORY}/releases/generate-notes",
        f"/repos/{REPOSITORY}/git/refs",
        f"/repos/{REPOSITORY}/releases",
    ]
    assert api.calls[-1] == api.writes[-1]
    assert (output / "release-notes.md").exists()


@pytest.mark.parametrize("filename", CALLER_FILES)
def test_missing_local_caller_prevents_all_remote_writes(prepared_release, filename):
    """Read both validated caller files before making any remote request."""
    api, output = prepared_release
    (output / filename).unlink()
    api.calls.clear()

    with pytest.raises(FileNotFoundError):
        release.create_draft(output, api, upload=api)

    assert not api.calls
    assert not (output / "release-notes.md").exists()


@pytest.mark.parametrize("upload_index", [0, 1])
@pytest.mark.parametrize("error", [http_error(503), urllib.error.URLError("upload disconnected")])
def test_upload_failure_stops_without_return_retry_cleanup_or_publication(prepared_release, upload_index, error):
    """Stop at the failed upload without retrying, cleaning up, or publishing the draft."""
    api, output = prepared_release
    api.upload_errors[CALLER_FILES[upload_index]] = error

    with pytest.raises(type(error)) as caught:
        release.create_draft(output, api, upload=api)

    assert caught.value is error
    assert [path for path, _ in api.writes] == [
        f"/repos/{REPOSITORY}/releases/generate-notes",
        f"/repos/{REPOSITORY}/git/refs",
        f"/repos/{REPOSITORY}/releases",
        *(f"/repos/{REPOSITORY}/releases/123/assets?name={filename}"
          for filename in CALLER_FILES[:upload_index + 1]),
    ]
    assert api.calls[-1] == api.uploads[-1]
    assert api.writes[2][1]["draft"] is True
    assert (output / "release-notes.md").exists()
    for filename in CALLER_FILES:
        assert (output / filename).is_file()


@pytest.mark.parametrize("upload_index", [0, 1])
@pytest.mark.parametrize("field,value", [
    (None, None),
    ("state", None), ("name", None), ("size", None),
    ("state", "starter"), ("name", "wrong.yml"),
    ("size", 0), ("size", 10**9), ("size", "123"),
])
def test_incomplete_or_mismatched_upload_metadata_stops_creation(prepared_release, upload_index, field, value):
    """Require exact uploaded asset identity and byte count before reporting success."""
    api, output = prepared_release
    filename = CALLER_FILES[upload_index]
    response = {"state": "uploaded", "name": filename, "size": (output / filename).stat().st_size}
    if field is None:
        response = {}
    elif value is None:
        response.pop(field)
    else:
        response[field] = value
    api.upload_responses[filename] = response

    with pytest.raises(ValueError, match="not uploaded completely"):
        release.create_draft(output, api, upload=api)

    assert [path for path, _ in api.writes] == [
        f"/repos/{REPOSITORY}/releases/generate-notes",
        f"/repos/{REPOSITORY}/git/refs",
        f"/repos/{REPOSITORY}/releases",
        *(f"/repos/{REPOSITORY}/releases/123/assets?name={name}"
          for name in CALLER_FILES[:upload_index + 1]),
    ]
    assert api.calls[-1] == api.uploads[-1]
    assert api.writes[2][1]["draft"] is True


@pytest.mark.parametrize("response", [
    {"draft": True},
    {"id": None, "draft": True},
    {"id": 0, "draft": True},
    {"id": -1, "draft": True},
    {"id": True, "draft": True},
    {"id": "123", "draft": True},
    {"id": 123.0, "draft": True},
    {"id": 123},
    {"id": 123, "draft": False},
    {"id": 123, "draft": "true"},
    {"id": 123, "draft": None},
])
def test_unidentified_or_non_draft_response_is_rejected_before_uploads(prepared_release, response):
    """Upload nothing unless GitHub identifies the newly created release as a draft."""
    api, output = prepared_release
    api.draft = {**response, "html_url": "https://github.com/example/release"}

    with pytest.raises(ValueError):
        release.create_draft(output, api, upload=api)

    assert not api.uploads
    assert [path for path, _ in api.writes] == [
        f"/repos/{REPOSITORY}/releases/generate-notes",
        f"/repos/{REPOSITORY}/git/refs",
        f"/repos/{REPOSITORY}/releases",
    ]
    assert api.calls[-1] == api.writes[-1]
    assert api.writes[-1][1]["draft"] is True

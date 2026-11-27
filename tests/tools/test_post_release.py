"""Exercise post-release maintenance with an isolated in-memory GitHub repository."""

import base64
import copy
import hashlib
import urllib.error
import urllib.parse

import pytest

from tools import caller, post_release

from .support import BASE, REPOSITORY, RecordedGitHub, http_error


RELEASE_SHA = "a" * 40
DEFAULT_SHA = "b" * 40
OLDER_SHA = "c" * 40
NEWER_SHA = "d" * 40
CALLER_PATH = ".github/workflows/cscs-review.yml"
RELEASE_ID = 123


def caller_bytes(revision=RELEASE_SHA, version="1.2.3", **kwargs):
    """Render the actual release caller rather than a hand-written substitute."""
    return caller.workflow(
        revision, allow_web=True, release_version=version, **kwargs).encode()


MALFORMED_CALLERS = (
    b"", b"not: [valid yaml", b"{}\n", b"\xff\xfe",
    caller.workflow(RELEASE_SHA, release_version="1.2.3").encode(),
    caller_bytes().replace(b"eth-cscs/cscs-oc-review/", b"contributor/fork/"),
    caller_bytes().replace(b"allow-web: true", b"allow-web: false"),
    caller_bytes().replace(b"allow-web: true", b"allow-web: true\n      extra: enabled"),
    caller_bytes(review_instructions_path="custom.md"),
    caller_bytes().replace(b"    secrets:\n", b"    secrets:\n      OTHER: ${{ secrets.OTHER }}\n"),
)


class GitHub(RecordedGitHub):
    """Store remote refs, trees, commits, PR history, and every attempted API write."""

    def __init__(self, version="1.2.3"):
        """Seed a published release and a default branch with forward ancestry."""
        super().__init__()
        self.version = version
        self.tag = "v" + version
        self.branch = "maintenance/post-release-" + self.tag
        self.release = {
            "id": RELEASE_ID, "tag_name": self.tag, "draft": False,
            "prerelease": False, "published_at": "2026-11-24T12:00:00Z",
        }
        self.failures = {}
        self.refs = {"tags/" + self.tag: RELEASE_SHA, "heads/main": DEFAULT_SHA}
        self.tag_objects = {}
        self.commits = {}
        self.trees = {}
        self.blobs = {}
        self.prs = []
        self.downloads = []
        self.pull_pages = None
        self.asset = caller_bytes(version=version)
        self.download_error = None
        self.add_commit(OLDER_SHA, {"VERSION": b"1.2.2\n"})
        released = {"VERSION": (version + "\n").encode(), "README.md": b"released\n"}
        self.add_commit(RELEASE_SHA, released, [OLDER_SHA])
        current = dict(released, **{"README.md": b"default branch advanced\n"})
        self.add_commit(DEFAULT_SHA, current, [RELEASE_SHA])

    def add_commit(self, sha, files, parents=()):
        """Create a fixture commit with a complete, independent file snapshot."""
        tree_sha = hashlib.sha1(("tree:" + sha).encode()).hexdigest()
        self.trees[tree_sha] = copy.deepcopy(files)
        self.commits[sha] = {
            "sha": sha, "tree": {"sha": tree_sha},
            "parents": [{"sha": parent} for parent in parents],
        }

    def files(self, sha):
        """Return the mutable file snapshot belonging to one fixture commit."""
        return self.trees[self.commits[sha]["tree"]["sha"]]

    def set_file(self, sha, path, content):
        """Change an initial fixture snapshot without recording a remote API write."""
        if content is None:
            self.files(sha).pop(path, None)
        else:
            self.files(sha)[path] = content

    def pull(self, *, state="open", merged=False, number=7, **changes):
        """Build canonical PR metadata, including history after head deletion."""
        result = {
            "number": number, "state": state,
            "merged_at": "2026-11-24T13:00:00Z" if merged else None,
            "html_url": f"https://github.com/{REPOSITORY}/pull/{number}",
            "head": {"ref": self.branch, "repo": {"full_name": REPOSITORY}},
            "base": {"ref": self.repository["default_branch"]},
        }
        result.update(changes)
        return result

    def orphan(self, *, files=None, parents=None):
        """Install a maintenance branch that exists without any pull request."""
        sha = "e" * 40
        default_sha = self.refs["heads/" + self.repository["default_branch"]]
        planned = copy.deepcopy(self.files(default_sha))
        major, minor, patch = self.version.split(".")
        planned["VERSION"] = f"{major}.{minor}.{int(patch) + 1}\n".encode()
        planned[CALLER_PATH] = self.asset
        if files:
            planned.update(files)
        self.add_commit(sha, planned, [default_sha] if parents is None else parents)
        self.refs["heads/" + self.branch] = sha
        return sha

    def download(self, tag):
        """Return immutable release bytes or an injected download failure."""
        self.downloads.append(tag)
        if self.download_error is not None:
            raise self.download_error
        return self.asset

    def is_ancestor(self, ancestor, descendant):
        """Traverse fixture commit parents without revisiting cycles."""
        pending = [descendant]
        seen = set()
        while pending:
            sha = pending.pop()
            if sha == ancestor:
                return True
            if sha not in seen:
                seen.add(sha)
                pending.extend(parent["sha"] for parent in self.commits[sha]["parents"])
        return False

    def __call__(self, path, payload=None):
        """Dispatch GitHub REST requests without network, local git, or local files."""
        self.record(path, payload)
        parsed = urllib.parse.urlsplit(path)
        route = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        failure = self.failures.get((route, payload is not None))
        if failure is not None:
            raise failure
        assert route.startswith(BASE), f"unexpected repository: {path}"
        route = route[len(BASE):]
        if payload is None:
            if route == "":
                return copy.deepcopy(self.repository)
            if route == f"/releases/{RELEASE_ID}":
                return copy.deepcopy(self.release)
            if route == "/pulls":
                assert query["state"] == ["all"]
                assert query["head"] == ["eth-cscs:" + self.branch]
                assert query["per_page"] == ["100"]
                page = int(query.get("page", ["1"])[0])
                if self.pull_pages is not None:
                    return copy.deepcopy(self.pull_pages[page])
                return copy.deepcopy(self.prs[(page - 1) * 100:page * 100])
            if route.startswith("/git/ref/"):
                ref = route[len("/git/ref/"):]
                if ref not in self.refs:
                    raise http_error(404)
                sha = self.refs[ref]
                return {"object": {"type": "tag" if sha in self.tag_objects else "commit", "sha": sha}}
            if route.startswith("/git/tags/"):
                return copy.deepcopy(self.tag_objects[route.rsplit("/", 1)[1]])
            if route.startswith("/git/commits/"):
                return copy.deepcopy(self.commits[route.rsplit("/", 1)[1]])
            if route.startswith("/contents/"):
                filename = route[len("/contents/"):]
                sha, = query["ref"]
                files = self.files(sha)
                if filename not in files:
                    raise http_error(404)
                return {
                    "type": "file", "encoding": "base64",
                    "content": base64.encodebytes(files[filename]).decode(),
                }
            if route.startswith("/compare/"):
                old, new = route[len("/compare/"):].split("...")
                if old == new:
                    status = "identical"
                elif self.is_ancestor(old, new):
                    status = "ahead"
                elif self.is_ancestor(new, old):
                    status = "behind"
                else:
                    status = "diverged"
                before, after = self.files(old), self.files(new)
                changed = sorted(name for name in before.keys() | after.keys()
                                 if before.get(name) != after.get(name))
                return {
                    "status": status,
                    "files": [{"filename": name, "status": "modified"} for name in changed],
                }
        elif route == "/git/blobs":
            assert payload["encoding"] == "base64"
            content = base64.b64decode(payload["content"], validate=True)
            sha = hashlib.sha1(b"blob:" + content).hexdigest()
            self.blobs[sha] = content
            return {"sha": sha}
        elif route == "/git/trees":
            files = copy.deepcopy(self.trees[payload["base_tree"]])
            for entry in payload["tree"]:
                assert entry["mode"] == "100644"
                assert entry["type"] == "blob"
                files[entry["path"]] = self.blobs[entry["sha"]]
            sha = hashlib.sha1(repr(sorted(files.items())).encode()).hexdigest()
            self.trees[sha] = files
            return {"sha": sha}
        elif route == "/git/commits":
            sha = hashlib.sha1(repr(payload).encode()).hexdigest()
            self.commits[sha] = {
                "sha": sha, "tree": {"sha": payload["tree"]},
                "parents": [{"sha": parent} for parent in payload["parents"]],
            }
            return {"sha": sha}
        elif route == "/git/refs":
            assert payload["ref"].startswith("refs/heads/")
            ref = payload["ref"][len("refs/"):]
            assert ref not in self.refs, "existing branch must never be overwritten"
            self.refs[ref] = payload["sha"]
            return {"ref": payload["ref"], "object": {"sha": payload["sha"]}}
        elif route == "/pulls":
            assert payload["head"] == self.branch
            assert payload["base"] == self.repository["default_branch"]
            assert payload["title"] and payload["body"]
            pr = self.pull()
            self.prs.append(pr)
            return copy.deepcopy(pr)
        pytest.fail(f"unexpected API request: {path}, {payload!r}")


def prepare(api, repository=REPOSITORY, release_id=RELEASE_ID):
    """Run maintenance orchestration solely against the recorded repository fake."""
    return post_release.prepare(repository, release_id, api=api, download=api.download)


def assert_result(result, api, state):
    """Require the expected state and a canonical upstream PR link."""
    assert result == {
        "state": state, "tag": api.tag,
        "url": f"https://github.com/{REPOSITORY}/pull/7",
    }


@pytest.mark.parametrize("version,next_version", [
    ("1.2.3", "1.2.4"), ("1.2.9", "1.2.10"), ("0.0.0", "0.0.1"),
])
def test_creation_preserves_asset_bytes_and_only_changes_two_files(
        version, next_version, tmp_path, monkeypatch):
    """Install exact asset bytes and advance only VERSION and the caller path."""
    monkeypatch.chdir(tmp_path)
    api = GitHub(version)
    api.asset += b"\n# Published bytes must survive installation unchanged.\n"
    before = copy.deepcopy(api.files(DEFAULT_SHA))
    result = prepare(api)
    assert_result(result, api, "created")
    assert api.downloads == [api.tag]
    head = api.refs["heads/" + api.branch]
    assert api.commits[head]["parents"] == [{"sha": DEFAULT_SHA}]
    assert api.files(head) == {
        **before, "VERSION": (next_version + "\n").encode(), CALLER_PATH: api.asset,
    }
    assert api.files(DEFAULT_SHA) == before
    assert api.refs["heads/main"] == DEFAULT_SHA
    assert api.refs["tags/" + api.tag] == RELEASE_SHA
    assert api.release["draft"] is False
    assert all("/releases" not in path for path, _ in api.writes)
    assert list(tmp_path.iterdir()) == []


def test_identical_default_and_release_commits_are_eligible():
    """Allow maintenance when the default branch still points at the released commit."""
    api = GitHub()
    api.refs["heads/main"] = RELEASE_SHA
    assert_result(prepare(api), api, "created")
    head = api.refs["heads/" + api.branch]
    assert api.commits[head]["parents"] == [{"sha": RELEASE_SHA}]


def test_current_repository_default_branch_is_used_instead_of_main():
    """Follow repository metadata for default branches containing path separators."""
    api = GitHub()
    api.repository["default_branch"] = "trunk/stable"
    api.refs["heads/trunk/stable"] = api.refs.pop("heads/main")
    assert_result(prepare(api), api, "created")
    assert api.prs[0]["base"]["ref"] == "trunk/stable"


def test_annotated_release_tag_resolves_to_the_release_commit():
    """Peel nested annotated tags before validating the asset's immutable pin."""
    api = GitHub()
    first, second = "1" * 40, "2" * 40
    api.refs["tags/" + api.tag] = first
    api.tag_objects[first] = {"object": {"type": "tag", "sha": second}}
    api.tag_objects[second] = {"object": {"type": "commit", "sha": RELEASE_SHA}}
    assert_result(prepare(api), api, "created")
    assert api.files(api.refs["heads/" + api.branch])[CALLER_PATH] == api.asset


@pytest.mark.parametrize("changes", [
    {"id": RELEASE_ID + 1}, {"draft": True}, {"prerelease": True},
    {"published_at": None}, {"published_at": ""},
    {"tag_name": "1.2.3"}, {"tag_name": "v01.2.3"},
    {"tag_name": "v1.2.3-rc.1"}, {"tag_name": "v1.2.3+build"},
    {"tag_name": "v1.2"}, {"tag_name": "v1.2.3\njobs: injected"},
])
def test_invalid_or_unpublished_release_is_rejected_without_writes(changes):
    """Reject nonstable or unpublished release metadata before modifying GitHub."""
    api = GitHub()
    api.release.update(changes)
    with pytest.raises(ValueError):
        prepare(api)
    assert not api.writes


@pytest.mark.parametrize("repository", ["contributor/fork", "ETH-CSCS/cscs-oc-review", ""])
def test_only_the_canonical_upstream_repository_is_accepted(repository):
    """Restrict maintenance to the exact canonical upstream repository name."""
    api = GitHub()
    with pytest.raises(ValueError):
        prepare(api, repository=repository)
    assert not api.writes


def test_repository_response_cannot_redirect_the_operation_to_a_fork():
    """Fail closed when API metadata disagrees with the requested upstream."""
    api = GitHub()
    api.repository["full_name"] = "contributor/fork"
    with pytest.raises(ValueError):
        prepare(api)
    assert not api.writes


@pytest.mark.parametrize("version", [b"1.2.4\n", b"v1.2.3\n", b"01.2.3\n", b"invalid\n"])
def test_released_source_version_must_match_the_stable_release_tag(version):
    """Require the released VERSION bytes to identify the selected stable tag."""
    api = GitHub()
    api.set_file(RELEASE_SHA, "VERSION", version)
    with pytest.raises(ValueError):
        prepare(api)
    assert not api.writes


@pytest.mark.parametrize("version", [b"1.2.4\n", b"1.2.2\n", b"invalid\n"])
def test_new_pr_requires_default_version_equal_to_released_version(version):
    """Treat an already-changed default VERSION as a conflict rather than overwriting it."""
    api = GitHub()
    api.set_file(DEFAULT_SHA, "VERSION", version)
    with pytest.raises(ValueError):
        prepare(api)
    assert not api.writes


def test_release_must_be_an_ancestor_of_the_current_default_branch():
    """Reject divergent release ancestry before installing a caller."""
    api = GitHub()
    api.commits[DEFAULT_SHA]["parents"] = [{"sha": OLDER_SHA}]
    with pytest.raises(ValueError):
        prepare(api)
    assert not api.writes


@pytest.mark.parametrize("asset", (*MALFORMED_CALLERS, caller_bytes(OLDER_SHA)))
def test_missing_malformed_or_mismatched_released_web_caller_blocks_writes(asset):
    """Validate release assets before creating any branch, blob, tree, commit, or PR."""
    api = GitHub()
    api.asset = asset
    with pytest.raises(ValueError):
        prepare(api)
    assert not api.writes


@pytest.mark.parametrize("error", [http_error(404), RuntimeError("asset download interrupted")])
def test_download_failure_never_regenerates_a_caller_or_creates_git_objects(error, monkeypatch):
    """Propagate missing assets and transport failures without generating replacement bytes."""
    api = GitHub()
    api.download_error = error

    def forbid_generation(*args, **kwargs):
        """Make fallback caller generation an explicit failure in download-error scenarios."""
        pytest.fail("failed downloads must never regenerate the published caller")

    monkeypatch.setattr(post_release, "workflow", forbid_generation)
    with pytest.raises(type(error)) as raised:
        prepare(api)
    assert raised.value is error
    assert api.downloads == [api.tag]
    assert not api.writes


@pytest.mark.parametrize("revision", [OLDER_SHA, RELEASE_SHA])
def test_canonical_existing_caller_can_advance_or_keep_its_release_pin(revision):
    """Allow canonical installed callers to advance or retain their release pin."""
    api = GitHub()
    api.set_file(DEFAULT_SHA, CALLER_PATH, caller_bytes(revision))
    assert_result(prepare(api), api, "created")
    assert api.files(api.refs["heads/" + api.branch])[CALLER_PATH] == api.asset


@pytest.mark.parametrize("release_id", [0, -1, "not-an-id", True])
def test_malformed_release_id_is_rejected_before_any_write(release_id):
    """Reject nonpositive and noninteger release IDs before any remote mutation."""
    api = GitHub()
    with pytest.raises(ValueError):
        prepare(api, release_id=release_id)
    assert not api.writes


def test_newer_installed_caller_cannot_be_downgraded():
    """Prevent a published older caller from downgrading a newer installed source."""
    api = GitHub()
    api.add_commit(NEWER_SHA, api.files(RELEASE_SHA), [RELEASE_SHA])
    api.commits[DEFAULT_SHA]["parents"] = [{"sha": NEWER_SHA}]
    api.set_file(DEFAULT_SHA, CALLER_PATH, caller_bytes(NEWER_SHA, "1.2.4"))
    with pytest.raises(ValueError):
        prepare(api)
    assert not api.writes


@pytest.mark.parametrize("installed", MALFORMED_CALLERS)
def test_custom_or_unrecognized_installed_caller_is_never_overwritten(installed):
    """Preserve customized installed workflows instead of silently replacing them."""
    api = GitHub()
    api.set_file(DEFAULT_SHA, CALLER_PATH, installed)
    with pytest.raises(ValueError):
        prepare(api)
    assert not api.writes
    assert api.files(DEFAULT_SHA)[CALLER_PATH] == installed


@pytest.mark.parametrize("state,merged,expected", [
    ("open", False, "open"), ("closed", True, "merged"), ("closed", False, "closed"),
])
@pytest.mark.parametrize("deleted_branch", [False, True])
def test_existing_pr_history_is_terminal_without_overwriting_or_reopening(
        state, merged, expected, deleted_branch):
    """Treat open, merged, and declined maintenance PR history as terminal on reruns."""
    api = GitHub()
    api.prs = [api.pull(state=state, merged=merged)]
    if not deleted_branch:
        api.orphan(files={CALLER_PATH: b"maintainer edited the branch\n"})
    api.set_file(DEFAULT_SHA, "VERSION", b"1.2.4\n")
    before = copy.deepcopy(api.refs)
    assert_result(prepare(api), api, expected)
    assert not api.writes
    assert api.refs == before


def test_pr_history_pagination_finds_a_deleted_merged_head():
    """Find merged PR history beyond the first page even after its branch was deleted."""
    api = GitHub()
    api.prs = [api.pull(number=100 + n, head={
        "ref": "other-branch", "repo": {"full_name": REPOSITORY},
    }) for n in range(100)]
    api.prs.append(api.pull(state="closed", merged=True))
    assert_result(prepare(api), api, "merged")
    assert not api.writes
    pages = [urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get("page", ["1"])
             for path, payload in api.calls
             if payload is None and urllib.parse.urlsplit(path).path == BASE + "/pulls"]
    assert ["2"] in pages


def test_duplicate_matching_pr_on_a_later_page_blocks_all_writes():
    """Inspect later history pages before deciding that a maintenance PR is unique."""
    api = GitHub()
    api.prs = [api.pull()]
    api.prs.extend(api.pull(number=100 + n, head={
        "ref": "other-branch", "repo": {"full_name": REPOSITORY},
    }) for n in range(99))
    api.prs.append(api.pull(number=8, state="closed", merged=True))
    with pytest.raises(ValueError, match="multiple maintenance PRs"):
        prepare(api)
    assert api.calls[-1][0].endswith("page=2")
    assert not api.writes


def test_malformed_pr_history_page_blocks_all_writes(tmp_path, monkeypatch):
    """Abort a malformed later history page without remote writes or generated files."""
    monkeypatch.chdir(tmp_path)
    api = GitHub()
    api.pull_pages = {1: [api.pull(number=100 + n, head={
        "ref": "other-branch", "repo": {"full_name": REPOSITORY},
    }) for n in range(100)], 2: None}
    with pytest.raises(ValueError, match="malformed GitHub REST page"):
        prepare(api)
    assert api.calls[-1][0].endswith("page=2")
    assert not api.writes
    assert list(tmp_path.iterdir()) == []


def test_exact_orphan_branch_is_recovered_by_creating_only_the_pull_request():
    """Recover an exact orphan commit without rewriting its branch or file bytes."""
    api = GitHub()
    head = api.orphan()
    before = copy.deepcopy(api.files(head))
    assert_result(prepare(api), api, "created")
    assert api.refs["heads/" + api.branch] == head
    assert api.files(head) == before
    assert [path for path, _ in api.writes] == [BASE + "/pulls"]


@pytest.mark.parametrize("changes", [
    {"VERSION": b"1.2.5\n"}, {CALLER_PATH: b"maintainer edits\n"},
    {"unexpected.txt": b"unrelated\n"}, {"README.md": b"unrelated modification\n"},
])
def test_modified_or_unrelated_orphan_branch_is_never_overwritten(changes):
    """Leave maintainer edits and unrelated orphan changes untouched on conflict."""
    api = GitHub()
    head = api.orphan(files=changes)
    before = copy.deepcopy(api.files(head))
    with pytest.raises(ValueError):
        prepare(api)
    assert not api.writes
    assert api.refs["heads/" + api.branch] == head
    assert api.files(head) == before


@pytest.mark.parametrize("parents", [[], [RELEASE_SHA], [DEFAULT_SHA, RELEASE_SHA]])
def test_orphan_requires_exactly_one_parent_at_the_current_default_head(parents):
    """Require orphan history to consist of one commit on the current default head."""
    api = GitHub()
    api.orphan(parents=parents)
    with pytest.raises(ValueError):
        prepare(api)
    assert not api.writes


@pytest.mark.parametrize("path", [
    "", f"/releases/{RELEASE_ID}", "/pulls", "/git/ref/tags/v1.2.3",
    "/git/ref/heads/main", f"/compare/{RELEASE_SHA}...{DEFAULT_SHA}",
    "/contents/VERSION", "/git/ref/heads/maintenance/post-release-v1.2.3",
    f"/git/commits/{DEFAULT_SHA}",
])
def test_api_read_errors_propagate_without_success_or_writes(path):
    """Propagate read failures rather than treating unavailable state as absence."""
    api = GitHub()
    error = http_error(503)
    api.failures[(BASE + path, False)] = error
    with pytest.raises(urllib.error.HTTPError) as raised:
        prepare(api)
    assert raised.value is error
    assert not api.writes


@pytest.mark.parametrize("path", ["/git/blobs", "/git/trees", "/git/commits", "/git/refs", "/pulls"])
def test_api_write_errors_propagate_without_bogus_pr_success(path):
    """Stop at a failed write without reporting a PR or advancing the default branch."""
    api = GitHub()
    error = http_error(503)
    api.failures[(BASE + path, True)] = error
    with pytest.raises(urllib.error.HTTPError) as raised:
        prepare(api)
    assert raised.value is error
    assert not api.prs
    assert api.refs["heads/main"] == DEFAULT_SHA
    assert api.writes[-1][0] == BASE + path


def test_pr_creation_failure_can_resume_from_the_exact_branch_without_overwrite():
    """Reuse the verified branch after PR creation fails instead of creating another commit."""
    api = GitHub()
    error = http_error(503)
    api.failures[(BASE + "/pulls", True)] = error
    with pytest.raises(urllib.error.HTTPError):
        prepare(api)
    head = api.refs["heads/" + api.branch]
    api.failures.clear()
    api.calls.clear()
    assert_result(prepare(api), api, "created")
    assert api.refs["heads/" + api.branch] == head
    assert [path for path, _ in api.writes] == [BASE + "/pulls"]


def test_default_branch_advancing_during_preparation_blocks_stale_branch_creation():
    """Recheck the captured default head before creating a stale maintenance branch."""
    api = GitHub()
    default_ref = BASE + "/git/ref/heads/main"
    reads = 0

    def advancing(path, payload=None):
        """Advance the remote default ref on its second read to model a concurrent change."""
        nonlocal reads
        if path == default_ref and payload is None:
            reads += 1
            if reads == 2:
                api.refs["heads/main"] = NEWER_SHA
        return api(path, payload)

    with pytest.raises(ValueError, match="default branch advanced"):
        post_release.prepare(REPOSITORY, RELEASE_ID, api=advancing, download=api.download)

    assert not api.writes
    assert "heads/" + api.branch not in api.refs


def test_cyclic_annotated_tag_fails_without_creating_git_objects():
    """Reject annotated-tag cycles before producing any remote Git objects."""
    api = GitHub()
    annotation = "f" * 40
    api.refs["tags/" + api.tag] = annotation
    api.tag_objects[annotation] = {"object": {"type": "tag", "sha": annotation}}

    with pytest.raises(ValueError, match="cycles"):
        prepare(api)

    assert not api.writes

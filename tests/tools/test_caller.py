"""Exercise literal instruction-path serialization and the caller CLI."""

from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from tools import caller


ROOT = Path(__file__).resolve().parents[2]
REVISION = "a" * 40


@pytest.mark.parametrize("allow_web", [False, True])
@pytest.mark.parametrize("path", [
    "docs/review rules.md",
    'rules/Review: "don\'t #skip" 📋.md',
    "rules/$(touch should-not-exist).md",
    r"rules/back\slash.md",
])
def test_literal_paths_cannot_change_workflow_structure(path, allow_web):
    """Keep hostile-looking path text confined to one literal workflow input."""
    generated = yaml.safe_load(caller.workflow(
        REVISION, allow_web=allow_web, review_instructions_path=path))
    expected = {"review-instructions-path": path}
    if allow_web:
        expected["allow-web"] = True
    assert generated["jobs"]["review"]["with"] == expected
    assert set(generated["jobs"]) == {"review"}


@pytest.mark.parametrize("path", [
    "rules/a\nb.md", "rules/a\rb.md", "rules/a\tb.md", "rules/a\x00b.md",
    "rules/a\x85b.md", "rules/a\u2028b.md", "rules/${{ secrets.CSCS_INFERENCE_KEY }}.md",
])
def test_paths_with_expression_or_line_injection_are_rejected(path):
    """Reject control characters and expressions before rendering caller YAML."""
    with pytest.raises(ValueError, match="printable literal"):
        caller.workflow(REVISION, review_instructions_path=path)


def test_cli_generates_a_literal_override_alongside_web_opt_in():
    """Expose the same safe literal-path and web controls through the module CLI."""
    path = 'docs/review: "project # rules".md'
    result = subprocess.run(
        [sys.executable, "-m", "tools.caller", "--revision", REVISION, "--allow-web",
         "--review-instructions-path", path],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    workflow = yaml.safe_load(result.stdout)
    assert workflow["jobs"]["review"]["with"] == {
        "allow-web": True, "review-instructions-path": path,
    }


@pytest.mark.parametrize("revision", [None, True, 123, "", "a" * 39, "a" * 41, "A" * 40])
def test_revision_requires_a_full_lowercase_sha(revision):
    """Reject noncanonical commit references instead of emitting movable pins."""
    with pytest.raises(ValueError, match="full lowercase"):
        caller.workflow(revision)

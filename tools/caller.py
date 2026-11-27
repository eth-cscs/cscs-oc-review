"""Render subscriber workflow YAML pinned to a full upstream commit SHA.

The optional source version is a comment, not the executable reference.
Generation does not install files or call GitHub.
"""

import argparse
import json
from common.identifiers import require_sha

from . import release_contract as contract


def workflow(
    revision: str,
    *,
    allow_web: bool = False,
    release_version: str | None = None,
    review_instructions_path: str = "",
) -> str:
    """Return caller YAML; reject invalid references or nonliteral instruction paths.

    revision is a lowercase, 40-character commit SHA; release_version adds only
    a version comment. The optional instruction path is emitted as a quoted
    literal; the runner validates its location and contents. allow_web opts into
    external search/fetch tools. Forward only CSCS_INFERENCE_KEY.
    """
    require_sha(revision, field="revision")
    if review_instructions_path and (
        not review_instructions_path.isprintable() or "${{" in review_instructions_path
    ):
        raise ValueError("review instruction path must be a printable literal without GitHub expressions")
    version_comment = "" if release_version is None else " # " + contract.version_tag(release_version)
    lines = [
        "name: CSCS review",
        "",
        "on:",
        "  # Conversation commands run the default branch's workflow, including for fork PRs.",
        "  issue_comment:",
        "    types: [created]",
        "",
        "permissions:",
        "  contents: read",
        "  pull-requests: write",
        "",
        "jobs:",
        "  review:",
        "    # Central authorization runs before per-PR cancellation or inference.",
        f"    uses: {contract.REVIEW_WORKFLOW}@{revision}{version_comment}",
    ]
    if allow_web or review_instructions_path:
        lines.append("    with:")
        if allow_web:
            lines.extend([
                "      # Search and fetch queries may disclose repository details to external services.",
                "      allow-web: true",
            ])
        if review_instructions_path:
            lines.extend([
                "      # Select instructions from the consumer default branch, not the PR head.",
                "      review-instructions-path: " + json.dumps(review_instructions_path, ensure_ascii=False),
            ])
    lines.extend([
        "    # Do not forward other repository or organization secrets.",
        "    secrets:",
        "      CSCS_INFERENCE_KEY: ${{ secrets.CSCS_INFERENCE_KEY }}",
    ])
    return "\n".join(lines) + "\n"


def main():
    """Print caller YAML to stdout; report invalid arguments through argparse."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True, help="published cscs-oc-review commit SHA")
    parser.add_argument("--allow-web", action="store_true", help="enable external web search and fetch tools")
    parser.add_argument("--release-version", help="source release version for the SHA reference comment")
    parser.add_argument(
        "--review-instructions-path",
        default="",
        help="required default-branch instruction file; omitted discovers optional .github/cscs-review.md",
    )
    args = parser.parse_args()
    try:
        print(workflow(
            args.revision,
            allow_web=args.allow_web,
            release_version=args.release_version,
            review_instructions_path=args.review_instructions_path,
        ), end="")
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()

# CSCS OpenCode review

## Purpose

This repository provides on-demand, advisory reviews of GitHub pull requests.
GitHub-hosted runners in each participating repository execute OpenCode, which uses the CSCS inference service to inspect PR changes.
The workflow posts the resulting review as a PR Conversation comment.
Findings require human review and are not required merge checks.

## Consumer setup

Install a small caller workflow that invokes [.github/workflows/review.yml](.github/workflows/review.yml), the only supported consumer entry point.

### Requirements

- The consumer must belong to `eth-cscs` and use GitHub.com.
  Public and private repositories can use the public shared workflows, subject to organization and repository Actions policies.
- Allow the caller's `contents: read` and `pull-requests: write` token permissions.
- Provide a `CSCS_INFERENCE_KEY` Actions secret at repository level or grant access to an organization secret.
  Use separate inference keys where repositories need independent budgets or revocation.

Workflow runs, artifacts, comments, and runner charges belong to the consumer.
No GitHub App, hosted receiver, PAT, registry credential, or CSCS-CI registration is required in a consumer.

### Install the caller

1. Review the changes and limitations in the [latest release](https://github.com/eth-cscs/cscs-oc-review/releases/latest).
2. Run this from the consumer repository root to download one caller as `.github/workflows/cscs-review.yml`.
   This overwrites an existing caller at that path.

   ```bash
   caller=cscs-review.yml # Use cscs-review-web.yml to enable web tools.
   mkdir -p .github/workflows &&
     curl --fail --location --silent --show-error \
       "https://github.com/eth-cscs/cscs-oc-review/releases/latest/download/$caller" \
       --output .github/workflows/cscs-review.yml
   ```

3. Review the downloaded file, preserve any custom inputs, and configure the inference secret and optional project instructions described below.
4. Merge the caller into the default branch, then [request a review](#request-and-result-behavior).

The caller must be on the default branch because GitHub dispatches `issue_comment` events from that branch; a caller in an unmerged PR will not run.
It pins the shared workflow to a full commit SHA, records the release in a version comment, and forwards only `CSCS_INFERENCE_KEY`.
Do not replace that explicit secret mapping with `secrets: inherit`.

### Project instructions

Add optional review-specific guidance at `.github/cscs-review.md` on the consumer's default branch.
For example:

```markdown
# Project review guidance

Prioritize correctness of halo exchanges and asynchronous buffer lifetimes.
Report performance concerns only with a concrete affected code path.
```

The reviewer also loads optional root `AGENTS.md` as general repository guidance.
To select another review file, add this input to the caller's existing `jobs.review` invocation:

```yaml
    with:
      review-instructions-path: "docs/review.md"
```

If `jobs.review` already has a `with` block, add `review-instructions-path` inside it alongside existing inputs such as `allow-web`.
Otherwise, add the block shown above.
An omitted or empty `review-instructions-path` selects the default file; a nonempty value replaces it with a required file.
It does not replace `AGENTS.md`.
Missing optional files are skipped, but a missing explicit replacement or an invalid present file stops preparation.

Both files come from the default branch, never from the PR head.
Paths must be printable and repository-relative, without absolute paths or parent traversal.
Files must be readable UTF-8 regular files within that checkout.
Symlinks to internal regular files are allowed; broken, looping, or escaping symlinks fail.

Conflict priority is review-specific instructions, then general repository guidance, then default reviewer guidance.
This defines model behavior, not a guarantee of compliance.
Runtime controls are independent of these instructions.

### Web access and private source

Web search and fetch tools are disabled by default.
To opt in, choose the web-enabled release caller or add `allow-web: true` to the same `with` mapping.
Search uses TinyFish without a separately configured search credential; service availability is best-effort.

Queries and fetched URLs can disclose repository details outside CSCS.
Disabling web tools does not restrict networking from the agent's shell.
Approve CSCS inference access and external-data policy before reviewing private source; this is not an exfiltration-proof sandbox.
Private-fork access depends on organization and fork-network permissions.
Private-fork and deleted-fork checkouts require a GitHub deployment check.
Submodules and Git LFS objects are not fetched, and the workflow does not obtain access to other private dependency repositories.

### Upgrades

To upgrade, download the selected caller from the new release and update the shared-workflow SHA and version comment in `.github/workflows/cscs-review.yml`, preserving optional inputs.
Review and merge the change.
The latest-release download URL selects a release; installed callers remain pinned to full SHAs and do not update automatically.

## Request and result behavior

### Request a review

Anyone with effective repository write access can post a new comment in an open PR's Conversation:

```text
/cscs-review Focus on cache invalidation and registry permissions.
```

The command must start the comment and is case-insensitive.
Separate optional focus text with a space, tab, CR, or LF.
Comment edits and inline diff comments do not trigger reviews.
Same-repository and public-fork PRs use the same entry point.
Authorization checks current GitHub permissions rather than `author_association`; maintain access maps to write access.
Noncommands and missing collaborators are skipped, while other permission API failures fail authorization.

### Results and retries

- An `eyes` reaction confirms receipt after entry-workflow authorization.
- The final review comment identifies the reviewed commit and links to the workflow run.
- A `confused` reaction indicates acknowledgement, review, or publication failure.
  Select **Re-run failed jobs** in Actions or post another command to retry.
  A successful retry removes that token user's failure reactions from the request comment.
- Cancellation leaves existing reactions unchanged.
  Clicking a reaction does not restart a run.
- Post another command after updating the PR head.
  Publication skips a result if its reviewed SHA no longer matches the current head, retaining the result as an artifact.
  The head check and comment creation are separate API calls, so a push between them can still race publication.

If OpenCode produces no final review text, the workflow fails without posting a review comment.
If a review is too long for a GitHub comment, the workflow does not post it; download the complete text from the `opencode-review` artifact.

### Context and concurrency

Each review receives PR metadata, the full merge-base diff, and all pages of previous reviews, Conversation comments, and inline threads, including replies and resolution/outdated state.
The complete context is mounted read-only at `/input/context.md`; inspection is instructed to read it in bounded ranges.
Missing or malformed history stops preparation rather than silently omitting discussion.
OpenCode is instructed to check earlier findings against the current code before reporting them again.

An authorized request replaces an earlier review of the same repository and PR.
Unauthorized comments cannot cancel reviews.
Different PRs and repositories run independently, subject to GitHub runner and inference-service capacity.
There is no organization-wide inference concurrency or spending controller.

## Limits and diagnostics

Consumers cannot override shared model settings, execution budgets, or executable plugins.
Consult these configuration sources for the current limits:

| Control | Definition |
| --- | --- |
| Model/provider selection, agent steps, output-token limits, reasoning effort, provider timeouts | [config/opencode.json](config/opencode.json) |
| Trusted inference upstream and allowed proxy routes | [config/credential-proxy.json](config/credential-proxy.json) |
| Phase deadlines, startup overhead, termination grace | [config/limits.json](config/limits.json) |
| Host execution deadline calculation and progress-reporting interval | [host/run.py](host/run.py) |
| GitHub job timeouts and artifact retention | [.github/workflows/review-worker.yml](.github/workflows/review-worker.yml) |

Agent `steps` count model iterations, not tool calls or total API requests.
Output-token limits apply per request, include reasoning, and do not define a review-wide token budget.
Provider timeouts limit waits for response headers and gaps between response chunks, not the overall duration of an active stream.
The host execution deadline includes phase deadlines, termination grace, and startup overhead.

Finalization requires an established session and at least one successful source-inspection tool call, which proves neither coverage nor correctness.
Inspection stopped by a deadline, iteration limit, or token truncation can finalize gathered evidence with an incomplete-coverage banner.
Other inspection errors and all finalization failures produce no publishable fallback.

The runner logs progress counts, tool summaries, and the current phase, not model text or tool arguments.
Workflow runs retain these artifacts:

| Artifact | Contents |
| --- | --- |
| `opencode-review` | Extracted final `review.md` |
| `opencode-review-diagnostics` | Counts, phase, exit/cleanup status, step outcomes, and bounded recent error excerpts |

Inspect `opencode-review-diagnostics` from the failed workflow run for details.
Diagnostics replace known secrets and values in common credential assignments with `[REDACTED]`.
Error excerpts can still contain source text.
Prepared context, raw model events, reasoning, and stderr are not uploaded.
The provider maps returned reasoning through `compatibility.reasoningField`; missing reasoning events do not establish that the model generated none.

## Implementation and maintenance

- [Design](docs/design.md): workflow jobs, trusted source, credential separation, and final-response validation.
- [Development](docs/development.md): source layout, local checks, container tests, and source-file conventions.
- [Releases](docs/releases.md): image publication, source releases, local caller generation, and upstream post-release maintenance.

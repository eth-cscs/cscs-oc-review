"""Run the trusted reviewer without exposing host credentials or writable source."""

import argparse
from collections import Counter, deque
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import quote, quote_plus

from host.context import git, worktree_root
from host.extract import EVENT_TYPES
from host.credential_proxy import CredentialProxy, load_policy
from host.container_policy import BASELINE_FLAGS


# Reporting cadence only; this does not impose a command or execution deadline.
_PROGRESS_REPORT_INTERVAL_SECONDS = 30
_FINISH_REASONS = {"stop", "end_turn", "length", "tool-calls", "content-filter", "error", "unknown"}
_TOOL_NAMES = {"execute", "glob", "grep", "question", "read", "shell",
               "skill", "subagent", "webfetch", "websearch"}
_TOOL_STATUSES = {"pending", "running", "completed", "error"}
_ERROR_MESSAGE_LIMIT = 1024
_ERROR_DETAIL_LIMIT = 8
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"""(?ix)\b(authorization|proxy-authorization|x-api-key|api[-_]?key|
    (?:access|refresh|auth)[-_]?token|token|password|secret)\b
    ["']?\s*[:=]\s*
    (?:"[^"]*"|'[^']*'|(?:bearer|basic)\s+[^\s,;&"']+|[^\s,;&"']+)"""
)
_AUTH_VALUE = re.compile(r"""(?i)\b(?:bearer|basic)\s+[^\s,;&"']+""")
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/\s?#]*@")


def _redact_error_text(text, secrets):
    """Remove known and credential-shaped secrets before normalizing public error text."""
    if not isinstance(text, str):
        return ""
    # Redact before truncation so a credential crossing the excerpt boundary
    # cannot leave its prefix in the public artifact.
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    text = _CREDENTIAL_ASSIGNMENT.sub(lambda match: f"{match[1]}=[REDACTED]", text)
    text = _AUTH_VALUE.sub("[REDACTED]", text)
    text = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)
    return " ".join("".join(char if char.isprintable() else " " for char in text).split())


def _error_detail(error, phase, secrets):
    """Keep bounded, redacted error details and only explicitly reported timeout evidence."""
    error = error if isinstance(error, dict) else {}
    kind = _redact_error_text(error.get("type"), secrets)[:80] or "unknown"
    message = _redact_error_text(error.get("message"), secrets)
    status = error.get("status")
    status = status if type(status) is int and 100 <= status <= 599 else None
    # Classify only an explicitly reported timeout, never infer one from silence
    # or elapsed time. A stream may time out after receiving HTTP 200.
    description = f"{kind} {message}".lower()
    timeout = "not_reported"
    if any(word in description for word in ("timed out", "timeout", "deadline exceeded")):
        if "response headers" in description:
            timeout = "response_headers"
        elif "chunk" in description:
            timeout = "response_chunk"
        elif "response to complete" in description:
            timeout = "response_complete"
        else:
            timeout = "unspecified"
    elif status in (408, 504):
        timeout = "http_status"
    return {
        "phase": phase, "type": kind, "http_status": status, "timeout": timeout,
        "message": message[:_ERROR_MESSAGE_LIMIT],
        "message_truncated": len(message) > _ERROR_MESSAGE_LIMIT,
    }


def _tool_result(part):
    """Summarize allowlisted tool states, including failures reported as completed."""
    if not isinstance(part, dict):
        return "other/other"
    name = part.get("tool")
    name = name if isinstance(name, str) and name in _TOOL_NAMES else "other"
    state = part.get("state")
    if not isinstance(state, dict):
        return f"{name}/other"
    status = state.get("status")
    status = status if isinstance(status, str) and status in _TOOL_STATUSES else "other"
    metadata = state.get("metadata")
    details = metadata.get("metadata") if isinstance(metadata, dict) else None
    if isinstance(details, dict):
        # Code Mode and shell failures can arrive with status=completed.
        exit_code = details.get("exit")
        if details.get("error") is True or (type(exit_code) is int and exit_code != 0):
            status = "error"
    return f"{name}/{status}"


def _report_progress(output, stopped, started, diagnostic=None, private_values=()):
    """Report fixed-label progress periodically and collect bounded private diagnostics."""
    previous = 0
    counts = Counter()
    tool_counts = Counter()
    last_finish = "none"
    phase = "none"
    errors = deque(maxlen=_ERROR_DETAIL_LIMIT)
    # These are host credentials, not values taken from untrusted model output.
    secrets = set(private_values)
    for name in ("CSCS_INFERENCE_KEY", "CSCS_API_KEY", "GH_TOKEN", "GITHUB_TOKEN",
                 "ACTIONS_RUNTIME_TOKEN"):
        if value := os.environ.get(name):
            secrets.add(value)
    secrets = sorted({
        variant for value in secrets
        for variant in (value, json.dumps(value, ensure_ascii=False)[1:-1],
                        quote(value, safe=""), quote_plus(value, safe=""))
    }, key=len, reverse=True)
    # A separate descriptor keeps reads from moving the container's stdout write position.
    with open(output.name, "rb") as events:
        while True:
            finished = stopped.wait(_PROGRESS_REPORT_INTERVAL_SECONDS)
            # Bound each scan so a busy producer cannot delay cancellation.
            size = os.stat(output.name).st_size
            while events.tell() < size:
                position = events.tell()
                line = events.readline(size - position)
                if not line:
                    break
                if not finished and not line.endswith(b"\n"):
                    # A write may stop mid-event; retry that line on the next report.
                    events.seek(position)
                    break
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    counts["invalid"] += 1
                    continue
                kind = event.get("type") if isinstance(event, dict) else None
                if not isinstance(kind, str) or kind not in EVENT_TYPES:
                    counts["unknown"] += 1
                    continue
                counts[kind] += 1
                if kind == "step_finish":
                    part = event.get("part")
                    reason = part.get("reason") if isinstance(part, dict) else None
                    last_finish = reason if isinstance(reason, str) and reason in _FINISH_REASONS else "other"
                elif kind == "tool_use":
                    tool_counts[_tool_result(event.get("part"))] += 1
                elif kind == "review_phase":
                    value = event.get("phase")
                    phase = value if isinstance(value, str) and value in ("inspection", "final") else "other"
                elif kind == "error":
                    errors.append(_error_detail(event.get("error"), phase, secrets))
            # Only fixed labels reach logs, never arguments, errors, output, or model text.
            summary = "; ".join(
                f"{label}=" + (", ".join(f"{name}:{count}" for name, count in sorted(counter.items())) or "none")
                for label, counter in (("events", counts), ("tools", tool_counts))
            )
            if errors:
                last_error = errors[-1]
                summary += (
                    f"; last_error_http_status={last_error['http_status'] or 'unknown'}"
                    f"; last_error_timeout={last_error['timeout']}"
                )
            if diagnostic is not None:
                diagnostic.update(events=dict(counts), tools=dict(tool_counts), phase=phase,
                                  last_finish=last_finish, stdout_bytes=size,
                                  errors=list(errors), summary_complete=finished)
            if finished:
                print(f"review runner: final {summary}; phase={phase}; last_finish={last_finish}", flush=True)
                return
            elapsed = int(time.monotonic() - started)
            print(f"review runner: running; elapsed={elapsed}s; stdout={size} bytes (+{size - previous}); "
                  f"{summary}; phase={phase}; last_finish={last_finish}", flush=True)
            previous = size

def _stop_process(process, grace):
    """Bound termination of the engine client and any local descendants."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return "completed"
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # Kill surviving descendants even if their process-group leader exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        return "failed"
    return "completed"


def _write_diagnostic(path, diagnostic):
    """Persist diagnostics in an owner-private artifact, never the public log."""
    with path.open("w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(diagnostic, output, sort_keys=True)
        output.write("\n")



def checkout_state(checkout):
    """Snapshot HEAD and all worktree changes for the post-cleanup integrity check."""
    return (git(checkout, "rev-parse", "--verify", "HEAD"),
            git(checkout, "status", "--porcelain=v1", "--untracked-files=all"))


def _validate_artifact_paths(checkout, inputs, artifacts):
    """Reject resolved artifact paths that overwrite source, inputs, or each other."""
    seen = set()
    for name, path in artifacts.items():
        if path == checkout or checkout in path.parents or path in inputs or path in seen:
            raise ValueError(f"{name} output must not overwrite the checkout, review inputs, or other artifacts")
        seen.add(path)


def run_review(engine, image, checkout, context, events, instructions, config_dir, proxy_image):
    """Run credential-separated review with readonly inputs and cleanup-before-integrity checks."""
    checkout = worktree_root(checkout)
    context = Path(context).resolve(strict=True)
    events = Path(events).resolve()
    # The workflow stages the merged consumer default-branch instruction bundle externally.
    instructions = Path(instructions).resolve(strict=True)
    if not instructions.is_file():
        raise ValueError("trusted instruction bundle must be a file")
    if checkout in instructions.parents:
        raise ValueError("instructions must be outside the PR checkout")
    config_dir = Path(config_dir).resolve(strict=True)
    if config_dir == checkout or checkout in config_dir.parents:
        raise ValueError("trusted configuration must be outside the PR checkout")
    config_paths = {name: (config_dir / name).resolve(strict=True)
                    for name in ("opencode.json", "limits.json", "credential-proxy.json")}
    for path in config_paths.values():
        if not path.is_file() or checkout in path.parents:
            raise ValueError("trusted configuration must contain files outside the PR checkout")
    # Read one snapshot so the host and image use the same limits even if files change later.
    configuration = {name: path.read_bytes() for name, path in config_paths.items()}
    limits = json.loads(configuration["limits.json"])
    fields = {"inspection_seconds", "finalization_seconds", "execution_overhead_seconds", "termination_grace_seconds"}
    if (not isinstance(limits, dict) or set(limits) != fields
            or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                   for value in limits.values())):
        raise ValueError("limits.json must define positive finite seconds for every execution limit")
    policy = load_policy(configuration["credential-proxy.json"])
    kill_grace = limits["termination_grace_seconds"]
    # Reserve termination grace for each phase, plus container startup and phase transitions.
    execution_seconds = (limits["inspection_seconds"] + limits["finalization_seconds"]
                         + 2 * kill_grace + limits["execution_overhead_seconds"])
    inputs = {context, instructions, *config_paths.values()}
    if not context.is_file():
        raise ValueError("context must be a file")
    diagnostics = events.with_suffix(".diagnostics.json").resolve()
    stderr = events.with_suffix(".stderr.log").resolve()
    _validate_artifact_paths(checkout, inputs, {
        "events": events, "diagnostics": diagnostics, "stderr": stderr,
    })
    if not os.environ.get("CSCS_INFERENCE_KEY"):
        raise ValueError("CSCS_INFERENCE_KEY is required")
    executable = shutil.which(engine)
    if executable is None:
        raise ValueError("container engine not found")
    before = checkout_state(checkout)
    events.parent.mkdir(parents=True, exist_ok=True)
    # Private host staging permits the unprivileged container UID to read context
    # even when the caller's RUNNER_TEMP files have restrictive owner-only modes.
    with tempfile.TemporaryDirectory(prefix="opencode-context-", dir=events.parent) as staging:
        started = time.monotonic()
        run_id = uuid.uuid4().hex
        proxy = CredentialProxy(
            executable, proxy_image, run_id, staging, policy,
            os.environ["CSCS_INFERENCE_KEY"], started + limits["execution_overhead_seconds"],
        )
        staged_context = Path(staging) / "context.md"
        staged_instructions = Path(staging) / "AGENTS.md"
        for source, destination in ((context, staged_context), (instructions, staged_instructions)):
            shutil.copyfile(source, destination)
            destination.chmod(0o444)
        staged_config = Path(staging) / "config"
        staged_config.mkdir(mode=0o755)
        for filename, content in configuration.items():
            if filename == "credential-proxy.json":
                continue
            destination = staged_config / filename
            destination.write_bytes(content)
            destination.chmod(0o444)
        name = f"opencode-review-{run_id}-reviewer"
        command = [
            executable, "run", "--rm", "--name", name, *BASELINE_FLAGS,
            "--label", proxy.label, "--network", proxy.network,
            "--user=1000:1000", "--workdir=/work",
            "--tmpfs=/work:rw,nosuid,nodev,mode=1777",
            "--tmpfs=/tmp:rw,nosuid,nodev,mode=1777",
            "--volume", f"{checkout}:/input/pr-source:ro",
            "--volume", f"{staged_context}:/input/context.md:ro",
            "--volume", f"{staged_instructions}:/instructions/AGENTS.md:ro",
            "--volume", f"{staged_config}:/opt/reviewer/config/opencode:ro",
            # Only run-scoped proxy credentials enter the reviewer, never the real inference key.
            "--env", "CSCS_REVIEW_PROXY_URL", "--env", "CSCS_REVIEW_PROXY_TOKEN", image,
        ]
        environment = dict(os.environ, CSCS_REVIEW_PROXY_URL=proxy.url,
                           CSCS_REVIEW_PROXY_TOKEN=proxy.token)
        diagnostic = {
            "status": "running", "failure": "none", "exit_code": None,
            "cleanup": "not_needed", "process_cleanup": "not_needed",
            "events": {}, "tools": {}, "phase": "none", "last_finish": "none",
            "stdout_bytes": 0, "summary_complete": False, "elapsed_seconds": 0,
            "proxy_status": "not_started",
            "errors": [],
        }
        _write_diagnostic(diagnostics, diagnostic)
        try:
            diagnostic["proxy_status"] = "starting"
            proxy.start()
            diagnostic["proxy_status"] = "ready"
            with events.open("w", encoding="utf-8") as output, stderr.open("w", encoding="utf-8") as errors:
                os.fchmod(output.fileno(), 0o600)
                os.fchmod(errors.fileno(), 0o600)
                print(f"review runner: starting isolated reviewer; deadline={execution_seconds:g}s; "
                      f"progress every {_PROGRESS_REPORT_INTERVAL_SECONDS}s "
                      "(byte counts and fixed event/tool summaries only)", flush=True)
                stopped = threading.Event()
                reporter = threading.Thread(target=_report_progress,
                                            args=(output, stopped, started, diagnostic, (proxy.token,)), daemon=True)
                reporter.start()
                process = None
                try:
                    # Raw stderr may contain source, model text, or credentials; never log it.
                    process = subprocess.Popen(command, stdout=output, stderr=errors,
                                               start_new_session=True, env=environment)
                    remaining = execution_seconds - (time.monotonic() - started)
                    diagnostic["exit_code"] = process.wait(timeout=max(0, remaining))
                except BaseException as error:
                    diagnostic["failure"] = (
                        "timeout" if isinstance(error, subprocess.TimeoutExpired)
                        else "interrupted" if isinstance(error, KeyboardInterrupt) else "runner_error")
                    if process is not None:
                        diagnostic["process_cleanup"] = _stop_process(process, kill_grace)
                        diagnostic["exit_code"] = process.returncode
                    if isinstance(error, subprocess.TimeoutExpired):
                        raise ValueError(f"isolated reviewer exceeded the {execution_seconds:g}s hard deadline") from error
                    raise
                finally:
                    stopped.set()
                    reporter.join(timeout=kill_grace)
                print(f"review runner: exited; status={diagnostic['exit_code']}; "
                      f"elapsed={int(time.monotonic() - started)}s; "
                      f"stdout={os.fstat(output.fileno()).st_size} bytes", flush=True)
            if diagnostic["exit_code"]:
                diagnostic["failure"] = "reviewer_exit"
                raise ValueError(f"isolated reviewer failed (exit {diagnostic['exit_code']})")
            diagnostic["status"] = "success"
        except BaseException as error:
            diagnostic["status"] = "failed"
            if diagnostic["failure"] == "none":
                diagnostic["failure"] = (
                    "timeout" if isinstance(error, subprocess.TimeoutExpired)
                    else "interrupted" if isinstance(error, KeyboardInterrupt) else "proxy_setup")
            if isinstance(error, subprocess.TimeoutExpired):
                raise ValueError("isolated reviewer exceeded its hard deadline during proxy setup") from error
            if isinstance(error, subprocess.CalledProcessError):
                raise ValueError("credential proxy setup failed") from error
            raise
        finally:
            # Cleanup is label-scoped and runs even after success or partial proxy startup.
            cleaned = proxy.cleanup()
            diagnostic["cleanup"] = "completed" if cleaned else "failed"
            if not cleaned:
                diagnostic["status"] = "failed"
                if diagnostic["failure"] == "none":
                    diagnostic["failure"] = "cleanup_failed"
                print("review runner: container cleanup failed", file=sys.stderr)
            # Cleanup errors must not skip the source-state check or diagnostic persistence.
            try:
                if checkout_state(checkout) != before:
                    diagnostic.update(status="failed", failure="checkout_changed")
                    raise ValueError("review changed the source checkout or HEAD")
            except BaseException:
                if diagnostic["failure"] != "checkout_changed":
                    diagnostic.update(status="failed", failure="checkout_check_error")
                raise
            finally:
                diagnostic["elapsed_seconds"] = int(time.monotonic() - started)
                _write_diagnostic(diagnostics, diagnostic)
            if diagnostic["failure"] == "cleanup_failed":
                raise ValueError("review resources could not be cleaned up")


def main(argv=None):
    """Parse trusted runner inputs and restore signal handling after execution or cancellation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="docker", help="Docker-compatible engine executable")
    parser.add_argument("--image", required=True)
    parser.add_argument("--proxy-image", required=True, help="digest-pinned credential proxy image")
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--instructions", required=True, help="externally staged consumer default-branch instructions")
    parser.add_argument("--config", required=True, help="trusted reviewer configuration directory outside the PR checkout")
    parser.add_argument("--events", required=True)
    args = parser.parse_args(argv)

    def interrupted(signum, frame):
        """Translate termination into the normal interruption and resource-cleanup path."""
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        run_review(args.engine, args.image, args.checkout, args.context, args.events,
                   args.instructions, args.config, args.proxy_image)
    except (OSError, ValueError) as error:
        parser.exit(1, f"review runner: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "review runner: interrupted\n")
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    main()

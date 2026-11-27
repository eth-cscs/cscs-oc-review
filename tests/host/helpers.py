"""Explicit fixture operations independent of production Git and CLI construction."""

import os
from pathlib import Path
import subprocess
import sys


TRUSTED_ROOT = Path(__file__).resolve().parents[2]


def git(directory, *args):
    """Run fixture Git with isolated global settings and deterministic commit identity."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Reviewer", GIT_AUTHOR_EMAIL="review@example.invalid",
        GIT_COMMITTER_NAME="Reviewer", GIT_COMMITTER_EMAIL="review@example.invalid",
    )
    return subprocess.run(["git", "-C", str(directory), *args], env=environment,
                          check=True, capture_output=True, text=True).stdout.strip()


def commit(directory, filename, content):
    """Commit one fixture file and return its immutable revision."""
    (directory / filename).write_text(content)
    git(directory, "add", "--", filename)
    git(directory, "commit", "-m", filename)
    return git(directory, "rev-parse", "HEAD")


def review_cli_args(*, engine, checkout, context, events, instructions, config,
                    image="unused", proxy_image="unused"):
    """Build an explicit module invocation using trusted external review inputs."""
    return [
        sys.executable, "-m", "host.run", "--engine", str(engine), "--image", image,
        "--proxy-image", proxy_image, "--checkout", str(checkout), "--context", str(context),
        "--events", str(events), "--instructions", str(instructions), "--config", str(config),
    ]


def completed_progress_scan(runner, path, content, *, private_values=()):
    """Finish a scan using the producer descriptor, including an unterminated final line."""
    diagnostic = {}
    with path.open("w+b") as output:
        os.write(output.fileno(), content)
        stopped = runner.threading.Event()
        stopped.set()
        runner._report_progress(output, stopped, runner.time.monotonic(), diagnostic, private_values)
    return diagnostic

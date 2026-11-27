import json
import os
import signal
import subprocess
import time

import pytest


from tests.host.helpers import TRUSTED_ROOT, completed_progress_scan, review_cli_args


@pytest.mark.parametrize("target", ["checkout-alias", "context", "instructions-alias", "configuration"])
def test_runner_cannot_overwrite_inputs(tmp_path, trusted_instructions, trusted_config, checkout, target):
    """Reject event-output aliases before source, context, guidance, or configuration can be truncated."""
    instructions = trusted_instructions
    source = checkout / "app.py"
    environment = dict(os.environ, CSCS_INFERENCE_KEY="local-path-safety-test")
    context = tmp_path / "context.md"
    context.write_text("important context\n")
    if target == "checkout-alias":
        alias = tmp_path / "looks-private"
        alias.symlink_to(checkout, target_is_directory=True)
        output = alias / "app.py"
    elif target == "context":
        output = context
    elif target == "configuration":
        output = trusted_config / "opencode.json"
    else:
        output = tmp_path / "instruction-alias"
        output.symlink_to(instructions)
    # /bin/false is a real failing executable, not a simulated container. If the
    # path guard regresses, opening stdout truncates the protected file before
    # that executable fails. No container or model credentials are needed.
    result = subprocess.run(review_cli_args(
        engine="/bin/false", checkout=checkout, context=context, events=output,
        instructions=instructions, config=trusted_config,
    ), cwd=TRUSTED_ROOT, env=environment, text=True, capture_output=True)
    assert result.returncode == 1
    assert source.read_text() == "important source\n"
    assert context.read_text() == "important context\n"
    assert instructions.read_text() == "trusted repository instructions\n"
    assert (trusted_config / "opencode.json").read_text() == "{}\n"


@pytest.mark.parametrize("alias", [False, True])
def test_runner_rejects_pr_supplied_instructions(tmp_path, trusted_config, checkout, review_input, alias):
    """Reject direct and aliased PR-owned guidance before starting the engine."""
    instructions = checkout / "AGENTS.md"
    instructions.write_text("untrusted PR instructions\n")
    if alias:
        link = tmp_path / "looks-trusted"
        link.symlink_to(instructions)
        instructions = link
    context = review_input
    output = tmp_path / "events.jsonl"
    environment = dict(os.environ, CSCS_INFERENCE_KEY="local-path-safety-test")
    # A successful real executable makes acceptance observable if the path guard regresses.
    result = subprocess.run(review_cli_args(
        engine="/bin/true", checkout=checkout, context=context, events=output,
        instructions=instructions, config=trusted_config,
    ), cwd=TRUSTED_ROOT, env=environment, text=True, capture_output=True)
    assert result.returncode == 1
    assert not output.exists()
    assert instructions.read_text() == "untrusted PR instructions\n"


@pytest.mark.parametrize("changed_source", [False, True])
def test_cleanup_timeout_preserves_failure_and_checks_source(
        tmp_path, review_inputs, runner, monkeypatch, capsys, engine_factory, changed_source):
    """Preserve reviewer failure precedence while checking source integrity after cleanup timeout."""
    engine, state = engine_factory(exit_code=1)
    monkeypatch.setenv("CSCS_INFERENCE_KEY", "local-cleanup-test")
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        """Inject container-removal timeout and an optional source mutation during cleanup."""
        if command[:2] == [str(engine), "rm"]:
            if changed_source:
                (review_inputs["checkout"] / "app.py").write_text("changed during cleanup\n")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(runner.subprocess, "run", run)
    expected = "checkout" if changed_source else "exit 1"
    with pytest.raises(ValueError, match=expected):
        runner.run_review(engine=str(engine), image="unused", proxy_image="unused", **review_inputs)
    assert "container cleanup failed" in capsys.readouterr().err
    diagnostic = json.loads((tmp_path / "events.diagnostics.json").read_text())
    assert diagnostic["status"] == "failed"
    assert diagnostic["failure"] == ("checkout_changed" if changed_source else "reviewer_exit")
    assert diagnostic["cleanup"] == "failed"
    assert diagnostic["exit_code"] == 1
    # Network cleanup must still be attempted after container removal times out.
    assert not json.loads(state.read_text())["network"]


def test_progress_reports_silence_without_exposing_event_text(tmp_path, runner, capsys):
    """Report quiet and active intervals using counts without exposing event text or secrets."""
    private_event = b'{"type":"reasoning","text":"private reasoning and credential marker"}\n'
    with (tmp_path / "events.jsonl").open("w+b") as output:
        class Reports:
            count = 0

            def wait(self, interval):
                """Advance deterministic report ticks and publish one private event between them."""
                self.count += 1
                if self.count == 2:
                    os.write(output.fileno(), private_event)
                return self.count > 2

        # Drive a quiet tick and an output tick without waiting for wall-clock time.
        runner._report_progress(output, Reports(), runner.time.monotonic())
    progress = capsys.readouterr().out
    assert "0 bytes (+0)" in progress
    assert f"{len(private_event)} bytes (+{len(private_event)})" in progress
    assert "reasoning:1" in progress
    assert "private reasoning" not in progress
    assert "credential marker" not in progress


def test_progress_waits_for_complete_events_and_counts_them_once(tmp_path, runner, capsys):
    """Defer incomplete event lines and count each completed event only once."""
    with (tmp_path / "events.jsonl").open("w+b") as output:
        class Reports:
            count = 0

            def wait(self, interval):
                """Split one completion event across deterministic report ticks before stopping."""
                self.count += 1
                if self.count == 1:
                    os.write(output.fileno(), b'{"type":"step_finish","part":{"reason":"length')
                elif self.count == 2:
                    os.write(output.fileno(), b'"}}\n')
                return self.count > 2

        runner._report_progress(output, Reports(), runner.time.monotonic())
    lines = capsys.readouterr().out.splitlines()
    assert "events=none" in lines[0]
    assert "step_finish:1" in lines[1] and "last_finish=length" in lines[1]
    assert "step_finish:1" in lines[2] and "step_finish:2" not in lines[2]
    assert "invalid" not in "\n".join(lines)


def test_progress_summarizes_final_errors_without_exposing_unknown_fields(tmp_path, runner, capsys):
    """Summarize errors and unknown labels without copying untrusted fields into logs."""
    completed_progress_scan(
        runner, tmp_path / "events.jsonl",
        b'{"type":"error","error":{"message":"private error marker"}}\n'
        b'{"type":"private event marker"}\n'
        b'{"type":"review_phase","phase":"private phase marker"}\n'
        b'{"type":"step_finish","part":{"reason":"private finish marker"}}',
    )
    progress = capsys.readouterr().out
    assert "error:1" in progress and "unknown:1" in progress
    assert "step_finish:1" in progress and "last_finish=other" in progress
    assert "phase=other" in progress
    assert "private" not in progress


@pytest.mark.parametrize(("error", "status", "timeout"), [
    ({"type": "provider.transport", "status": 200,
      "message": "Timed out waiting for the response to complete"}, 200, "response_complete"),
    ({"type": "provider.transport",
      "message": "Timed out waiting for response headers"}, None, "response_headers"),
    ({"type": "provider.transport", "message": "Response chunk timeout"}, None, "response_chunk"),
    ({"type": "TimeoutError", "message": "Request failed"}, None, "unspecified"),
    ({"type": "provider.api", "status": 504}, 504, "http_status"),
    ({"type": "provider.api", "status": 413, "message": "Request too large"}, 413, "not_reported"),
    ({"type": "provider.api", "status": 503, "message": "Unavailable"}, 503, "not_reported"),
    ({"type": [], "status": True, "message": {}}, None, "not_reported"),
    (None, None, "not_reported"),
])
def test_error_diagnostics_distinguish_timeouts_and_http_failures(
        tmp_path, runner, capsys, error, status, timeout):
    """Classify explicit timeout evidence separately from HTTP failures and successful stream status."""
    diagnostic = completed_progress_scan(
        runner, tmp_path / "events.jsonl",
        b"".join(json.dumps(event).encode() + b"\n" for event in [
            {"type": "review_phase", "phase": "inspection", "state": "start"},
            {"type": "error", "error": error},
        ]),
    )
    detail, = diagnostic["errors"]
    assert detail["phase"] == "inspection"
    assert detail["http_status"] == status
    assert detail["timeout"] == timeout
    assert diagnostic["events"]["error"] == 1
    progress = capsys.readouterr().out
    assert "provider.transport" not in progress
    assert f"last_error_http_status={status or 'unknown'}" in progress
    assert f"last_error_timeout={timeout}" in progress


def test_error_artifact_redacts_credentials_before_truncation(
        tmp_path, runner, monkeypatch, capsys):
    """Redact credential variants and unknown secret shapes before bounded private persistence."""
    secret = 'inference-key/"with spaces'
    monkeypatch.setenv("CSCS_INFERENCE_KEY", secret)
    marker = "UNRETAINED_RESPONSE_PAYLOAD"
    message = (
        f"{secret} {json.dumps(secret)[1:-1]} {runner.quote(secret, safe='')} "
        f"{runner.quote_plus(secret, safe='')} "
        'Authorization: Bearer unknown-bearer-value; {"apiKey": "unknown-api-value"} '
        "password='unknown password value'; https://user:unknown-url-value@example.invalid "
        "Bearer standalone-value\n"
    )
    # A separate message places a secret across the output boundary.
    diagnostic = completed_progress_scan(
        runner, tmp_path / "events.jsonl",
        b"".join(json.dumps({"type": "error", "error": {
            "type": f"provider.{secret}", "message": text, "status": 500,
            "responseBody": marker, "headers": {"Authorization": marker},
        }}).encode() + b"\n" for text in (message, "x" * 1020 + secret + " trailing detail")),
    )
    path = tmp_path / "events.diagnostics.json"
    runner._write_diagnostic(path, diagnostic)
    raw = path.read_text()
    detail, boundary = json.loads(raw)["errors"]
    assert detail["type"] == "provider.[REDACTED]"
    assert detail["message_truncated"] is False
    assert boundary["message_truncated"] is True
    assert len(boundary["message"]) == 1024
    assert "[REDACTED]" in detail["message"]
    for forbidden in (secret, "inference-key", "unknown-bearer-value", "unknown-api-value",
                      "unknown password value", "unknown-url-value", "standalone-value", marker):
        assert forbidden not in raw
    assert path.stat().st_mode & 0o777 == 0o600
    progress = capsys.readouterr().out
    assert "provider." not in progress and "REDACTED" not in progress


def test_error_diagnostics_keep_recent_failures_with_bounded_storage(tmp_path, runner):
    """Count every failure while retaining only the most recent bounded error details."""
    diagnostic = completed_progress_scan(
        runner, tmp_path / "events.jsonl",
        b"".join(json.dumps({"type": "error", "error": {
            "type": "provider.api", "status": status,
        }}).encode() + b"\n" for status in range(500, 510)),
    )
    assert diagnostic["events"]["error"] == 10
    assert [detail["http_status"] for detail in diagnostic["errors"]] == list(range(502, 510))



@pytest.mark.parametrize(("tool", "state", "result"), [
    ("read", {"status": "completed"}, "read/completed"),
    ("read", {"status": "error"}, "read/error"),
    ("execute", {"status": "completed", "metadata": {"metadata": {"error": True}}}, "execute/error"),
    ("shell", {"status": "completed", "metadata": {"metadata": {"exit": 128}}}, "shell/error"),
    ("shell", {"status": "completed", "metadata": {"metadata": {"exit": 0}}}, "shell/completed"),
    ("private tool marker", {"status": "private status marker"}, "other/other"),
])
def test_progress_classifies_tool_results_without_exposing_payload(
        tmp_path, runner, capsys, tool, state, result):
    """Summarize allowlisted tool outcomes without logging private arguments or results."""
    state = dict(state, input={"command": "private command marker"},
                 output="private output marker", error="private error marker")
    event = {"type": "tool_use", "part": {"tool": tool, "state": state}}
    completed_progress_scan(runner, tmp_path / "events.jsonl", json.dumps(event).encode() + b"\n")
    progress = capsys.readouterr().out
    assert f"tools={result}:1" in progress
    assert "private" not in progress


@pytest.mark.parametrize("exit_code", [0, 7])
def test_runner_retains_private_events_and_sanitized_diagnostics(
        tmp_path, review_inputs, runner, monkeypatch, capfd, engine_factory, exit_code):
    """Preserve private artifacts while independently asserting isolation, readonly mounts, and cleanup."""
    review_inputs["context"].write_text("private context marker\n")
    engine, state = engine_factory(exit_code=exit_code)
    monkeypatch.setenv("CSCS_INFERENCE_KEY", "private credential marker")
    events = review_inputs["events"]
    if exit_code:
        with pytest.raises(ValueError, match=f"exit {exit_code}"):
            runner.run_review(engine=str(engine), image="unused", proxy_image="unused", **review_inputs)
    else:
        runner.run_review(engine=str(engine), image="unused", proxy_image="unused", **review_inputs)
    diagnostic_path = tmp_path / "events.diagnostics.json"
    raw = diagnostic_path.read_text()
    diagnostic = json.loads(raw)
    assert diagnostic["status"] == ("failed" if exit_code else "success")
    assert diagnostic["failure"] == ("reviewer_exit" if exit_code else "none")
    assert diagnostic["exit_code"] == exit_code
    assert diagnostic["events"] == {"reasoning": 1, "unknown": 1}
    assert diagnostic["summary_complete"] is True
    assert diagnostic["elapsed_seconds"] >= 0
    assert diagnostic["cleanup"] == "completed"
    resources = json.loads(state.read_text())
    assert not resources["containers"] and not resources["network"]
    reviewer, = [call for call in resources["calls"] if call[0] == "run" and "--detach" not in call]
    proxy, = [call for call in resources["calls"] if call[0] == "run" and "--detach" in call]
    # Deliberately do not derive expected flags from production's shared tuple.
    for command in (reviewer, proxy):
        assert "--pull=never" in command
        assert "--read-only" in command
        assert "--cap-drop=ALL" in command
        assert "--security-opt=no-new-privileges" in command
        assert all(command[index + 1].endswith(":ro")
                   for index, flag in enumerate(command) if flag == "--volume")
    assert "--user=1000:1000" in reviewer
    assert "--env" in reviewer and "CSCS_REVIEW_PROXY_TOKEN" in reviewer
    assert "--env=XDG_CONFIG_HOME=/tmp/config" in proxy
    assert "--env=XDG_CONFIG_HOME=/tmp/config" not in reviewer
    assert "CSCS_REVIEW_PROXY_TOKEN" not in proxy
    assert diagnostic_path.stat().st_mode & 0o777 == 0o600
    assert "private model marker" in events.read_text()
    stderr_path = tmp_path / "events.stderr.log"
    assert "private stderr credential marker" in stderr_path.read_text()
    assert stderr_path.stat().st_mode & 0o777 == 0o600
    captured = capfd.readouterr()
    assert "private" not in raw + captured.out + captured.err


def test_hanging_reviewer_is_killed_and_container_cleanup_is_bounded(
        tmp_path, review_inputs, runner, monkeypatch, engine_factory):
    """Enforce the hard deadline, kill the engine client, and clean resources without source mutation."""
    engine, state = engine_factory(hang=True)
    monkeypatch.setenv("CSCS_INFERENCE_KEY", "local-deadline-test")
    (review_inputs["config_dir"] / "limits.json").write_text(json.dumps({
        "inspection_seconds": 0.1, "finalization_seconds": 0.1,
        "execution_overhead_seconds": 1, "termination_grace_seconds": 0.1,
    }))
    started = time.monotonic()
    with pytest.raises(ValueError, match="hard deadline"):
        runner.run_review(engine=str(engine), image="unused", proxy_image="unused", **review_inputs)
    assert time.monotonic() - started < 5
    diagnostic = json.loads((tmp_path / "events.diagnostics.json").read_text())
    assert diagnostic["status"] == "failed"
    assert diagnostic["failure"] == "timeout"
    assert diagnostic["exit_code"] == -signal.SIGKILL
    assert diagnostic["process_cleanup"] == "completed"
    assert diagnostic["cleanup"] == "completed"
    resources = json.loads(state.read_text())
    assert not resources["containers"] and not resources["network"]
    assert (review_inputs["checkout"] / "app.py").read_text() == "important source\n"


@pytest.mark.parametrize("suffix", ["diagnostics.json", "stderr.log"])
def test_private_outputs_cannot_overwrite_inputs(
        tmp_path, review_inputs, runner, monkeypatch, suffix):
    """Reject diagnostic and stderr aliases to protected review inputs before event creation."""
    (tmp_path / f"events.{suffix}").symlink_to(review_inputs["context"])
    monkeypatch.setenv("CSCS_INFERENCE_KEY", "local-path-safety-test")
    with pytest.raises(ValueError, match=suffix.split(".")[0]):
        runner.run_review(engine="/bin/true", image="unused", proxy_image="unused", **review_inputs)
    assert review_inputs["context"].read_text() == "review input\n"
    assert not (tmp_path / "events.jsonl").exists()


@pytest.mark.parametrize(("alias", "target", "error"), [
    ("diagnostics", "events", "diagnostics"),
    ("stderr", "events", "stderr"),
    ("stderr", "diagnostics", "stderr"),
])
def test_private_outputs_cannot_collide_with_other_artifacts(
        tmp_path, review_inputs, runner, alias, target, error):
    """Reject collisions among event, diagnostic, and stderr destinations without truncation."""
    artifacts = {
        "events": tmp_path / "events.jsonl",
        "diagnostics": tmp_path / "events.diagnostics.json",
        "stderr": tmp_path / "events.stderr.log",
    }
    artifacts[target].write_text("existing private artifact\n")
    artifacts[alias].symlink_to(artifacts[target])
    with pytest.raises(ValueError, match=error):
        runner.run_review(engine="/bin/true", image="unused", proxy_image="unused",
                          **(review_inputs | {"events": artifacts["events"]}))
    assert artifacts[target].read_text() == "existing private artifact\n"
    assert review_inputs["context"].read_text() == "review input\n"


@pytest.mark.parametrize("artifact", ["diagnostics", "stderr"])
def test_private_outputs_cannot_overwrite_source(
        review_inputs, runner, artifact):
    """Reject diagnostic and stderr aliases into the source tree before creating events."""
    events = review_inputs["events"]
    suffix = ".diagnostics.json" if artifact == "diagnostics" else ".stderr.log"
    events.with_suffix(suffix).symlink_to(review_inputs["checkout"] / "app.py")
    with pytest.raises(ValueError, match=artifact):
        runner.run_review(engine="/bin/true", image="unused", proxy_image="unused", **review_inputs)
    assert (review_inputs["checkout"] / "app.py").read_text() == "important source\n"
    assert not events.exists()


@pytest.mark.parametrize("target", ["directory", "opencode.json", "limits.json", "credential-proxy.json"])
def test_runner_rejects_pr_configuration_symlinks(
        tmp_path, review_inputs, runner, target):
    """Reject PR-owned configuration reached through directory or individual-file aliases."""
    checkout = review_inputs["checkout"]
    trusted_config = review_inputs["config_dir"]
    if target == "directory":
        alias = tmp_path / "config-alias"
        alias.symlink_to(checkout, target_is_directory=True)
        config = alias
    else:
        source = checkout / target
        source.write_bytes((trusted_config / target).read_bytes())
        (trusted_config / target).unlink()
        (trusted_config / target).symlink_to(source)
        config = trusted_config
    output = review_inputs["events"]
    with pytest.raises(ValueError, match="trusted configuration"):
        runner.run_review(engine="/bin/true", image="unused", proxy_image="unused",
                          **(review_inputs | {"config_dir": config}))
    assert not output.exists()


@pytest.mark.parametrize("changes", [
    {"inspection_seconds": 0},
    {"finalization_seconds": True},
    {"termination_grace_seconds": float("inf")},
    {"execution_seconds": 17},
])
def test_invalid_limits_fail_before_launch(
        review_inputs, runner, changes):
    """Reject incomplete or nonpositive, boolean, and nonfinite execution limits before engine launch."""
    path = review_inputs["config_dir"] / "limits.json"
    limits = json.loads(path.read_text())
    path.write_text(json.dumps(limits | changes))
    output = review_inputs["events"]
    with pytest.raises(ValueError, match="limits.json"):
        runner.run_review(engine="/bin/true", image="unused", proxy_image="unused", **review_inputs)
    assert not output.exists()




def test_partial_proxy_startup_cleans_network_without_starting_reviewer(
        review_inputs, runner, monkeypatch, engine_factory):
    """Clean partial proxy resources after setup fails without launching the reviewer."""
    engine, state = engine_factory()
    monkeypatch.setenv("CSCS_INFERENCE_KEY", "canary-key")
    control = runner.CredentialProxy._run

    def fail_after_network_creation(self, arguments, **kwargs):
        """Fail immediately after a successful network creation to exercise partial-startup cleanup."""
        result = control(self, arguments, **kwargs)
        if arguments[:2] == ["network", "create"]:
            raise subprocess.CalledProcessError(1, arguments)
        return result

    monkeypatch.setattr(runner.CredentialProxy, "_run", fail_after_network_creation)
    events = review_inputs["events"]
    with pytest.raises(ValueError, match="proxy setup failed"):
        runner.run_review(engine=str(engine), image="unused", proxy_image="unused", **review_inputs)
    assert not events.exists()
    resources = json.loads(state.read_text())
    assert not resources["network"] and not resources["containers"]
    diagnostic = json.loads(events.with_suffix(".diagnostics.json").read_text())
    assert diagnostic["failure"] == "proxy_setup"
    assert diagnostic["cleanup"] == "completed"


def test_cleanup_failure_cannot_report_a_successful_review(
        review_inputs, runner, monkeypatch, engine_factory):
    """Turn an otherwise successful review into failure if its resources cannot be removed."""
    engine, state = engine_factory()
    monkeypatch.setenv("CSCS_INFERENCE_KEY", "canary-key")
    control = runner.CredentialProxy._run

    def fail_container_listing(self, arguments, **kwargs):
        """Timeout container discovery while allowing independent network cleanup to continue."""
        if arguments[0] == "ps":
            raise subprocess.TimeoutExpired(arguments, 0)
        return control(self, arguments, **kwargs)

    monkeypatch.setattr(runner.CredentialProxy, "_run", fail_container_listing)
    events = review_inputs["events"]
    with pytest.raises(ValueError, match="could not be cleaned up"):
        runner.run_review(engine=str(engine), image="unused", proxy_image="unused", **review_inputs)
    diagnostic = json.loads(events.with_suffix(".diagnostics.json").read_text())
    assert diagnostic["status"] == "failed" and diagnostic["failure"] == "cleanup_failed"
    assert not json.loads(state.read_text())["network"]


def test_sigterm_reaps_reviewer_and_proxy_resources(
        tmp_path, trusted_instructions, trusted_config, checkout, review_input, engine_factory):
    """Route SIGTERM through interruption diagnostics and cleanup without leaking credentials."""
    instructions = trusted_instructions
    engine, state = engine_factory(hang=True)
    context = review_input
    events = tmp_path / "events.jsonl"
    process = subprocess.Popen(review_cli_args(
        engine=engine, checkout=checkout, context=context, events=events,
        instructions=instructions, config=trusted_config,
    ), cwd=TRUSTED_ROOT, env=dict(os.environ, CSCS_INFERENCE_KEY="canary-key"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                containers = json.loads(state.read_text())["containers"]
                if any(name.endswith("-reviewer") for name in containers):
                    break
            except json.JSONDecodeError:
                pass
            if process.poll() is not None or time.monotonic() >= deadline:
                pytest.fail("reviewer did not start before cancellation")
            time.sleep(0.01)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 130
    diagnostic = json.loads(events.with_suffix(".diagnostics.json").read_text())
    assert diagnostic["failure"] == "interrupted"
    assert diagnostic["cleanup"] == "completed"
    resources = json.loads(state.read_text())
    assert not resources["containers"] and not resources["network"]
    assert "canary-key" not in stdout + stderr


def test_cli_imports_only_from_trusted_package_root(
        tmp_path, trusted_instructions, trusted_config, checkout, review_input, engine_factory):
    """Keep PR-local module names from replacing trusted host imports in module invocation."""
    marker = tmp_path / "untrusted-import"
    for module in ("context.py", "extract.py", "credential_proxy.py", "run.py"):
        (checkout / module).write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('PR import')\n")
    engine, state = engine_factory()
    events = tmp_path / "events.jsonl"
    result = subprocess.run(review_cli_args(
        engine=engine, checkout=checkout, context=review_input, events=events,
        instructions=trusted_instructions, config=trusted_config,
    ), cwd=TRUSTED_ROOT, env=dict(os.environ, CSCS_INFERENCE_KEY="canary-key"),
        text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    resources = json.loads(state.read_text())
    assert not resources["containers"] and not resources["network"]

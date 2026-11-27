import json

import pytest


from host import extract


def event(kind, message="final", session="parent", **part):
    """Encode a native event with explicit parent session and assistant message identity."""
    return json.dumps({"type": kind, "sessionID": session,
                       "part": {"messageID": message, **part}})


def text(value, message="final", session="parent", part_id="text", **changes):
    """Encode a completed text part with caller-selected identity and overrides."""
    return event("text", message, session, id=part_id, text=value, time={"end": 1}, **changes)


def phase(name, state, session="parent"):
    """Encode one runner-owned phase transition for a specified parent session."""
    return json.dumps({"type": "review_phase", "phase": name, "state": state, "sessionID": session})


def inspection():
    """Build a successful inspection containing notes that must stay private."""
    return [phase("inspection", "start"), text("Private inspection notes", "inspection"),
            event("tool_use", "inspection"), phase("inspection", "end")]


def review(final):
    """Wrap final-phase events in a complete successful inspection and final lifecycle."""
    return [*inspection(), phase("final", "start"), *final, phase("final", "end")]


def test_only_explicit_final_parent_response_is_published():
    """Publish final text only, excluding inspection, tools, and reasoning."""
    lines = review([
        event("step_start"),
        event("reasoning", text="Private reasoning"),
        text("## Findings", part_id="heading"),
        text("- P1: incorrect boundary."),
        event("step_finish", reason="stop"),
    ])
    assert extract.extract_review(lines) == "## Findings\n\n- P1: incorrect boundary.\n"


@pytest.mark.parametrize("position", [2, 5])
def test_foreign_session_in_either_phase_is_rejected(position):
    """Reject foreign-session output regardless of the active review phase."""
    lines = review([text("Otherwise complete review"), event("step_finish", reason="stop")])
    lines.insert(position, text("Unexpected session", session="other"))
    with pytest.raises(ValueError, match="session"):
        extract.extract_review(lines)


@pytest.mark.parametrize("reason", ["stop", "end_turn"])
def test_verified_final_response_without_step_start_is_accepted(reason):
    """Accept a completed parent final response without a redundant start event."""
    assert extract.extract_review(review([
        text("No actionable findings."), event("step_finish", reason=reason),
    ])) == "No actionable findings.\n"


@pytest.mark.parametrize("started", [False, True])
def test_reconciled_response_without_finish_requires_successful_phase_end(started):
    """Require successful finalization even when native completion markers are reconciled away."""
    final = [event("reasoning", text="Private reasoning", time={"end": 1}),
             text("No actionable findings.")]
    if started:
        final.insert(0, event("step_start"))
    assert extract.extract_review(review(final)) == "No actionable findings.\n"
    with pytest.raises(ValueError, match="stopped before completing"):
        extract.extract_review(review(final)[:-1])


@pytest.mark.parametrize("first_finish", [None, "stop"])
@pytest.mark.parametrize("continuation_started", [False, True])
def test_continuation_cannot_replace_an_earlier_final_message(
        first_finish, continuation_started):
    """Reject a second assistant message before it can replace the original final response."""
    final = [text("## Findings\n\n- Initial finding.")]
    if first_finish is not None:
        final.append(event("step_finish", reason=first_finish))
    if continuation_started:
        final.append(event("step_start", message="continuation"))
    final.append(text("- Later finding.", message="continuation"))
    final.append(event("step_finish", message="continuation", reason="stop"))
    with pytest.raises(ValueError, match="changed the assistant message ID"):
        extract.extract_review(review(final))


def test_repeated_step_start_preserves_reconciled_text():
    """Keep reconciled final text across repeated starts for the same assistant message."""
    assert extract.extract_review(review([
        text("## Findings", part_id="heading"),
        event("step_start"),
        text("- Initial finding."),
        event("step_start"),
        event("step_finish", reason="stop"),
    ])) == "## Findings\n\n- Initial finding.\n"


def test_updated_text_part_is_not_duplicated():
    """Replace a text part in place rather than publishing both revisions."""
    assert extract.extract_review(review([
        event("step_start"), text("Partial"), text("Complete review"),
        event("step_finish", reason="stop"),
    ])) == "Complete review\n"


@pytest.mark.parametrize("lines", [
    inspection(),
    [*inspection(), phase("final", "start"), text("Looks complete but was interrupted"),
     event("step_finish", reason="stop")],
    [phase("inspection", "start"), text("An inspection review"), phase("final", "start")],
    [*inspection(), phase("final", "start", "different-session"), text("Wrong session")],
    review([text("A final review"), event("step_finish", reason="stop")]) + [text("Unexpected extra output")],
    [json.dumps({"type": "review_phase", "phase": [], "state": "start"})],
    # An unmarked old-style stream must never bypass explicit finalization.
    [event("step_start"), text("Inspection masquerading as a final review")],
])
def test_missing_reordered_or_mismatched_phases_are_rejected(lines):
    """Reject streams that cannot prove one complete ordered parent review lifecycle."""
    with pytest.raises(ValueError):
        extract.extract_review(lines)


@pytest.mark.parametrize("suffix", [
    [event("tool_use"), text("A later response must not hide the tool attempt", "later"),
     event("step_finish", message="later", reason="stop")],
    [event("step_finish", reason="length")],
    [event("step_finish", reason="error")],
    [event("step_finish", reason="tool-calls")],
    [event("step_finish", reason=None)],
    [event("step_finish")],
    [json.dumps({"type": "error", "error": {"message": "provider failed"}})],
])
def test_partial_or_tool_using_final_responses_are_not_published(suffix):
    """Reject final-phase tools, provider errors, and unsuccessful completion reasons."""
    with pytest.raises(ValueError):
        extract.extract_review(review([
            event("step_start"), text("Intermediate text"), *suffix,
            event("step_finish", reason="stop"),
        ]))


def test_inspection_error_cannot_be_hidden_by_a_final_response():
    """Keep inspection provider errors fatal even if final text appears complete."""
    lines = review([text("An otherwise complete review"), event("step_finish", reason="stop")])
    lines.insert(2, json.dumps({"type": "error"}))
    with pytest.raises(ValueError):
        extract.extract_review(lines)


@pytest.mark.parametrize("final", [
    [event("step_finish", reason="stop")],
    [text("  "), event("step_finish", reason="stop")],
    ["not JSON"],
    ["[]"],
    [json.dumps({"type": "unexpected"})],
    [json.dumps({"type": []})],
    [event("text", id="part", text="Incomplete", time={"start": 1}),
     event("step_finish", reason="stop")],
])
def test_empty_corrupt_and_incomplete_final_output_is_rejected(final):
    """Reject malformed events, unfinished text parts, and empty final responses."""
    with pytest.raises(ValueError):
        extract.extract_review(review(final))


def test_failed_extraction_preserves_existing_output(tmp_path):
    """Preserve an existing review artifact when extraction validation fails."""
    events = tmp_path / "events.jsonl"
    final = [text("Initial findings."), text("Continuation fragment.", message="continuation")]
    events.write_text("\n".join(review(final)) + "\n")
    output = tmp_path / "review.md"
    output.write_text("existing review\n")
    with pytest.raises(SystemExit) as error:
        extract.main(["--events", str(events), "--output", str(output)])
    assert error.value.code == 1
    assert output.read_text() == "existing review\n"


def test_cli_cannot_overwrite_event_stream(tmp_path):
    """Reject output aliases to the native event input before writing."""
    events = tmp_path / "events.jsonl"
    original = "\n".join(review([text("A review"), event("step_finish", reason="stop")])) + "\n"
    events.write_text(original)
    alias = tmp_path / "alias"
    alias.symlink_to(events)
    with pytest.raises(SystemExit) as error:
        extract.main(["--events", str(events), "--output", str(alias)])
    assert error.value.code == 1
    assert events.read_text() == original


def incomplete_review(final):
    """Mark only inspection end as interrupted while leaving final events unchanged."""
    lines = review(final)
    marker = json.loads(lines[3])
    marker["incomplete"] = True
    lines[3] = json.dumps(marker)
    return lines


@pytest.mark.parametrize("finish", [None, "stop"])
def test_interrupted_inspection_qualifies_completed_final_review(finish):
    """Require the exact consumer warning before findings from interrupted inspection."""
    final = [text("## Findings\n\n- Boundary bug.")]
    if finish is not None:
        final.append(event("step_finish", reason=finish))
    result = extract.extract_review(incomplete_review(final))
    assert result == (
        "> Inspection was interrupted. Findings cover only the code inspected; "
        "this is not a clean bill of health for the remaining changes.\n\n"
        "## Findings\n\n- Boundary bug.\n"
    )


@pytest.mark.parametrize("final", [
    [text("Truncated"), event("step_finish", reason="length")],
    [text("Truncated"), event("step_finish", reason="length"), text("Later text", message="later"),
     event("step_finish", message="later", reason="stop")],
    [text("Failure"), event("step_finish", reason="error"), text("Later text", message="later"),
     event("step_finish", message="later", reason="stop")],
    [text("Review"), event("tool_use", session="child-session"), event("step_finish", reason="stop")],
    [text("Review"), json.dumps({"type": "error", "error": {"message": "private failure"}}),
     event("step_finish", reason="stop")],
    [event("text", id="text", text="Partial", time={"start": 1}),
     event("step_finish", reason="stop")],
    [event("step_finish", reason="stop")],
])
def test_incomplete_inspection_never_relaxes_final_validation(final):
    """Keep every final-response validation requirement after interrupted inspection."""
    with pytest.raises(ValueError):
        extract.extract_review(incomplete_review(final))


def test_interrupted_inspection_with_unfinished_final_is_not_publishable():
    """Reject a missing successful final end even when final text has a stop marker."""
    with pytest.raises(ValueError):
        extract.extract_review(incomplete_review([
            text("Looks finished"), event("step_finish", reason="stop"),
        ])[:-1])


@pytest.mark.parametrize(("marker_index", "value"), [
    (0, True), (3, "true"), (3, 1), (3, None), (4, True), (7, True),
])
def test_incomplete_marker_is_only_boolean_inspection_end(marker_index, value):
    """Reject misplaced or nonboolean interruption markers rather than weakening validation."""
    lines = review([text("A review"), event("step_finish", reason="stop")])
    marker = json.loads(lines[marker_index])
    marker["incomplete"] = value
    lines[marker_index] = json.dumps(marker)
    with pytest.raises(ValueError, match="incomplete"):
        extract.extract_review(lines)


def test_false_incomplete_marker_does_not_qualify_complete_inspection():
    """Leave a complete review unqualified when the interruption marker is false."""
    lines = incomplete_review([text("A review"), event("step_finish", reason="stop")])
    marker = json.loads(lines[3])
    marker["incomplete"] = False
    lines[3] = json.dumps(marker)
    assert extract.extract_review(lines) == "A review\n"


def test_incomplete_marker_cannot_hide_inspection_error():
    """Keep an inspection error fatal despite an allowed interruption marker."""
    lines = incomplete_review([text("A review"), event("step_finish", reason="stop")])
    lines.insert(3, json.dumps({"type": "error"}))
    with pytest.raises(ValueError, match="reported an error"):
        extract.extract_review(lines)

from dataclasses import FrozenInstanceError, replace

import pytest

from halcyon.console_events import ConsoleEvent, CONSOLE_TERMINAL_STATES, MAX_CONSOLE_TEXT
from halcyon.mcp_local.run_tracker import TERMINAL_RUN_STATES


def event(**kwargs):
    return ConsoleEvent("event-1", "message", "assistant", "codex", "Hello", **kwargs)


def test_console_event_is_an_immutable_internal_value():
    item = event(run_id="run", parent_run_id="parent", session_id="session", operation_id="op")
    with pytest.raises(FrozenInstanceError):
        item.text = "changed"
    assert item.raw_text is None
    assert item.details is None
    assert item.run_id == "run"
    assert item.parent_run_id == "parent"
    assert item.session_id == "session"
    assert item.operation_id == "op"
    assert CONSOLE_TERMINAL_STATES == TERMINAL_RUN_STATES


def test_text_copy_details_and_provenance_are_bounded_and_scrubbed():
    unsafe = "sk-private-marker /Users/private/file OPENAI_API_KEY=plain-secret "
    item = replace(
        event(),
        text=unsafe + "x" * 10_000,
        raw_text=unsafe + "x" * 10_000,
        details=unsafe + "x" * 10_000,
        source=unsafe + "s" * 100,
        local_origin=unsafe,
    )
    assert len(item.text) == MAX_CONSOLE_TEXT
    assert len(item.raw_text) == MAX_CONSOLE_TEXT
    assert len(item.details) == MAX_CONSOLE_TEXT
    assert len(item.source) <= 80
    assert "sk-private-marker" not in repr(item)
    assert "/Users/private/file" not in repr(item)
    assert "plain-secret" not in repr(item)


@pytest.mark.parametrize(
    "command",
    [
        "echo hello",
        "/panic\n/panic",
        "/panic\r/panic",
        "/coach load /tmp/private.mid",
        "/x " + "a" * 8_000,
    ],
)
def test_missing_exact_safe_replay_is_not_reconstructed(command):
    assert event(replay_command=command).replay_command is None


def test_safe_private_replay_preserves_exact_input():
    assert event(replay_command="/synth set cutoff 0.25").replay_command == "/synth set cutoff 0.25"

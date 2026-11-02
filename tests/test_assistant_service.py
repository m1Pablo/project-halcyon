from threading import Event
from unittest.mock import MagicMock

import pytest

from halcyon.assistant import AssistantResult, HalcyonAssistant, MAX_ASSISTANT_HISTORY_ENTRIES
from halcyon.assistant_service import AssistantService, MAX_SESSIONS
from halcyon.mcp_local.run_tracker import RunTracker


def service(assistant=None, tracker=None, command=None):
    return AssistantService(
        assistant or HalcyonAssistant(client=MagicMock()),
        tracker or RunTracker(),
        command=command or MagicMock(),
        state=lambda: {"audio": {"ready": True}},
    )


def finish(svc, started):
    svc._worker.join(timeout=2)
    assert not svc._worker.is_alive()
    return svc.tracker.wait_for_run(started["run_id"])


def test_actual_outcomes_are_retained_and_sessions_are_isolated():
    assistant = HalcyonAssistant(client=MagicMock())
    assistant.ask = MagicMock(
        return_value=AssistantResult(
            "Tone changed.",
            [
                {
                    "tool": "run_slash_command",
                    "leaf": "/synth set",
                    "status": "completed",
                    "ok": True,
                }
            ],
        )
    )
    svc = service(assistant)
    first = svc.start("make it darker")
    result = finish(svc, first)
    assert result["terminal"]
    assert result["result"]["reply"] == "Tone changed."
    second = svc.start("a bit more", session_id=first["session_id"])
    finish(svc, second)
    history = assistant.ask.call_args.kwargs["history"]
    assert history[0]["content"] == "make it darker"
    assert "completed" in history[1]["content"]
    third = svc.start("new conversation")
    finish(svc, third)
    assert assistant.ask.call_args.kwargs["history"] == []
    assert third["session_id"] != first["session_id"]


def test_inflight_request_is_busy_even_when_parent_timed_out():
    entered, release = Event(), Event()
    assistant = HalcyonAssistant(client=MagicMock())

    def ask(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=2)
        return AssistantResult("Late response.")

    assistant.ask = ask
    svc = service(assistant)
    first = svc.start("one")
    assert entered.wait(timeout=1)
    try:
        svc.tracker.mark_timeout(first["run_id"])
        busy = svc.start("two")
        assert busy["started"] is False
        assert busy["error"]["code"] == "ASSISTANT_BUSY"
        assert busy["active_run_id"] == first["run_id"]
    finally:
        release.set()
    result = finish(svc, first)
    assert result["status"] == "timed_out"
    assert "result" not in result


def test_unknown_session_is_explicit_and_does_not_execute():
    svc = service()
    result = svc.start("continue", session_id="a" * 32)
    assert result["error"]["code"] == "SESSION_NOT_FOUND"
    assert svc.tracker.get_state()["runs_total"] == 0


def test_waits_for_real_child_terminal_result_before_returning_to_model():
    tracker = RunTracker()
    assistant = HalcyonAssistant(client=MagicMock())
    captured = []

    def command(text, parent, timeout):
        child, status = tracker.create_run(
            command="/coach replay", source="llm", timeout_s=timeout, parent_run_id=parent
        )
        tracker.mark_running(child)
        tracker.mark_completed(
            child, success=True, result={"ok": True, "coach": {"phase": "ready"}}
        )
        return 202, status

    def ask(_goal, *, control, **_kwargs):
        captured.append(control("run_slash_command", {"command": "/coach replay"}))
        return AssistantResult("Replay ended.")

    assistant.ask = ask
    svc = service(assistant, tracker, command)
    started = svc.start("replay")
    finish(svc, started)
    assert captured[0]["status"] == "completed"
    assert captured[0]["result"]["coach"]["phase"] == "ready"
    assert captured[0]["parent_run_id"] == started["run_id"]


def test_foreign_deduplicated_run_is_not_claimed_or_retried():
    assistant = HalcyonAssistant(client=MagicMock())
    captured = []

    def ask(_goal, *, control, **_kwargs):
        captured.append(control("run_slash_command", {"command": "/coach replay"}))
        return AssistantResult("Another operation is active.")

    assistant.ask = ask
    command = MagicMock(
        return_value=(200, {"deduplicated": True, "run_id": "a" * 32, "status": "running"})
    )
    svc = service(assistant, command=command)
    finish(svc, svc.start("replay"))
    assert captured[0]["ok"] is False
    assert "Another operation" in captured[0]["error"]
    command.assert_called_once()


def test_sessions_history_and_replies_are_bounded_and_credentials_scrubbed():
    assistant = HalcyonAssistant(api_key="unprefixed-private-key", client=MagicMock())
    assistant.ask = MagicMock(return_value=AssistantResult("sk-private-response " + "x" * 3_000))
    svc = service(assistant)
    current = None
    for _ in range(6):
        started = svc.start("unprefixed-private-key " + "g" * 1_500, session_id=current)
        finish(svc, started)
        current = started["session_id"]
    history = svc._sessions[current]
    assert len(history) == MAX_ASSISTANT_HISTORY_ENTRIES
    assert all(len(item["content"]) <= 1_000 for item in history)
    assert "unprefixed-private-key" not in str(history)
    assert "sk-private-response" not in str(history)
    for _ in range(MAX_SESSIONS):
        finish(svc, svc.start("new"))
    assert len(svc._sessions) == MAX_SESSIONS
    assert current not in svc._sessions


def test_missing_key_is_visible_in_terminal_verdict(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    svc = service(HalcyonAssistant())
    result = finish(svc, svc.start("help"))
    assert result["status"] == "failed"
    assert result["result"]["stopped_reason"] == "missing_key"
    assert result["verdict"]["needs_attention"]


def test_closing_prevents_new_actions():
    svc = service()
    svc.close()
    assert svc.start("help")["error"]["code"] == "ASSISTANT_CLOSED"


def test_typed_request_progress_and_reply_have_one_emitter_and_existing_identity():
    assistant = HalcyonAssistant(api_key="private-key-marker", client=MagicMock())
    tracker = RunTracker()
    observed, messages = [], []
    tracker.subscribe(observed.append)
    legacy = MagicMock()

    def ask(_goal, *, progress, **_kwargs):
        progress("private-key-marker sk-credential /Users/private/path " + "x" * 1_000)
        return AssistantResult("## Reply\n```python\nprint('safe')\n```")

    assistant.ask = MagicMock(side_effect=ask)
    svc = AssistantService(
        assistant,
        tracker,
        command=MagicMock(),
        state=lambda: {},
        display=legacy,
        event_sink=messages.append,
    )
    started = svc.start("private-key-marker help", source="codex")
    result = finish(svc, started)
    assert result["status"] == "completed"
    assistant.ask.assert_called_once()
    legacy.assert_not_called()
    assert [item.role for item in messages] == ["user", "assistant"]
    assert all(item.kind == "message" for item in messages)
    assert all(item.source == "codex" for item in messages)
    assert all(item.run_id == started["run_id"] for item in messages)
    assert all(item.session_id == started["session_id"] for item in messages)
    assert messages[0].status is None
    assert messages[-1].status == "completed"
    assert messages[-1].text.startswith("## Reply")
    progress_events = [item for item in observed if item.kind == "log"]
    assert len(progress_events) == 1
    assert len(progress_events[0].text) <= 500
    assert "private-key-marker" not in repr(messages + observed)
    assert "sk-credential" not in repr(messages + observed)
    assert "/Users/private/path" not in repr(messages + observed)
    assert len({item.event_id for item in messages + observed}) == len(messages + observed)


def test_event_sink_failure_cannot_change_assistant_result_or_invoke_legacy_display():
    assistant = HalcyonAssistant(client=MagicMock())
    assistant.ask = MagicMock(return_value=AssistantResult("Finished."))
    legacy = MagicMock()
    svc = AssistantService(
        assistant,
        RunTracker(),
        command=MagicMock(),
        state=lambda: {},
        display=legacy,
        event_sink=MagicMock(side_effect=RuntimeError("UI unavailable")),
    )
    assert finish(svc, svc.start("help"))["status"] == "completed"
    legacy.assert_not_called()


def test_late_response_after_close_or_timeout_does_not_emit_success():
    for close in (False, True):
        entered, release = Event(), Event()
        assistant = HalcyonAssistant(client=MagicMock())

        def ask(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=2)
            return AssistantResult("Late success.")

        assistant.ask = ask
        messages = []
        svc = AssistantService(
            assistant,
            RunTracker(),
            command=MagicMock(),
            state=lambda: {},
            event_sink=messages.append,
        )
        started = svc.start("help")
        assert entered.wait(timeout=1)
        if close:
            svc.close()
        else:
            svc.tracker.mark_timeout(started["run_id"])
        release.set()
        result = finish(svc, started)
        assert result["status"] == ("cancelled" if close else "timed_out")
        assert [item.role for item in messages] == ["user"]


def test_typed_failure_reply_retains_failed_status():
    assistant = HalcyonAssistant(client=MagicMock())
    assistant.ask = MagicMock(side_effect=RuntimeError("private provider details"))
    messages = []
    svc = AssistantService(
        assistant, RunTracker(), command=MagicMock(), state=lambda: {}, event_sink=messages.append
    )
    assert finish(svc, svc.start("help"))["status"] == "failed"
    assert messages[-1].status == "failed"
    assert messages[-1].severity == "error"
    assert "private provider details" not in repr(messages)


def test_dependency_failure_has_safe_actionable_error_without_raw_exception():
    assistant = HalcyonAssistant(client=MagicMock())
    assistant.ask = MagicMock(side_effect=ImportError("sk-private /Users/private/dependency"))
    messages = []
    svc = AssistantService(
        assistant, RunTracker(), command=MagicMock(), state=lambda: {}, event_sink=messages.append
    )
    result = finish(svc, svc.start("help"))
    assert result["status"] == "failed"
    assert "dependency is unavailable" in result["error"]
    assert "dependency is unavailable" in messages[-1].text
    assert "sk-private" not in repr(messages)
    assert "/Users/private" not in repr(messages)


@pytest.mark.parametrize(
    "failure", [RuntimeError("synthetic failure"), ImportError("synthetic dependency")]
)
def test_accepted_first_session_can_retry_after_exception(failure):
    assistant = HalcyonAssistant(client=MagicMock())
    assistant.ask = MagicMock(side_effect=[failure, AssistantResult("Recovered.")])
    svc = service(assistant)
    first = svc.start("first request")
    assert finish(svc, first)["status"] == "failed"
    assert svc._sessions[first["session_id"]] == []
    retry = svc.start("try again", session_id=first["session_id"])
    assert retry["started"]
    assert retry["session_id"] == first["session_id"]
    assert retry["run_id"] != first["run_id"]
    assert finish(svc, retry)["status"] == "completed"
    assert assistant.ask.call_count == 2
    assert assistant.ask.call_args.kwargs["history"] == []


def test_accepted_first_session_can_retry_after_timeout_without_late_history():
    entered, release = Event(), Event()
    assistant = HalcyonAssistant(client=MagicMock())

    def first_ask(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=2)
        return AssistantResult("Late uncommitted reply.")

    assistant.ask = MagicMock(side_effect=first_ask)
    svc = service(assistant)
    first = svc.start("first request")
    assert entered.wait(timeout=1)
    try:
        assert svc.tracker.mark_timeout(first["run_id"])
        busy = svc.start("not yet", session_id=first["session_id"])
        assert busy["error"]["code"] == "ASSISTANT_BUSY"
    finally:
        release.set()
    assert finish(svc, first)["status"] == "timed_out"
    assert svc._sessions[first["session_id"]] == []
    assistant.ask = MagicMock(return_value=AssistantResult("Recovered after timeout."))
    retry = svc.start("try again", session_id=first["session_id"])
    assert retry["started"]
    assert finish(svc, retry)["status"] == "completed"
    assert assistant.ask.call_args.kwargs["history"] == []
    assert svc.tracker.get_status(first["run_id"])["status"] == "timed_out"


def test_failed_new_sessions_obey_existing_capacity_and_unknown_ids_still_fail():
    assistant = HalcyonAssistant(client=MagicMock())
    assistant.ask = MagicMock(side_effect=RuntimeError("synthetic failure"))
    svc = service(assistant)
    sessions = []
    for _ in range(MAX_SESSIONS + 3):
        started = svc.start("new failed conversation")
        sessions.append(started["session_id"])
        assert finish(svc, started)["status"] == "failed"
        assert len(svc._sessions) <= MAX_SESSIONS
    assert list(svc._sessions) == sessions[-MAX_SESSIONS:]
    assert all(history == [] for history in svc._sessions.values())
    before = assistant.ask.call_count
    missing = svc.start("unknown remains rejected", session_id=sessions[0])
    assert missing["error"]["code"] == "SESSION_NOT_FOUND"
    assert assistant.ask.call_count == before


def test_request_message_has_no_stale_running_label_while_command_tracks_lifecycle():
    entered, release = Event(), Event()
    assistant = HalcyonAssistant(client=MagicMock())

    def ask(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=2)
        return AssistantResult("Synthetic completed reply.")

    assistant.ask = ask
    messages = []
    svc = AssistantService(
        assistant, RunTracker(), command=MagicMock(), state=lambda: {}, event_sink=messages.append
    )
    started = svc.start("synthetic request")
    try:
        assert entered.wait(timeout=1)
        assert len(messages) == 1
        assert messages[0].role == "user"
        assert messages[0].status is None
        assert svc.tracker.get_status(started["run_id"])["status"] == "running"
    finally:
        release.set()
    assert finish(svc, started)["status"] == "completed"
    assert messages[0].status is None
    assert messages[-1].role == "assistant"
    assert messages[-1].status == "completed"

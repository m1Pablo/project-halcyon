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


def test_model_diagnostics_reach_tracked_result_and_display():
    import httpx
    from openai import BadRequestError

    client = MagicMock()
    client.responses.create.side_effect = BadRequestError(
        "private provider message",
        response=httpx.Response(
            400,
            request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
            headers={"x-request-id": "req_test"},
        ),
        body={"code": "unsupported_parameter", "param": "input"},
    )
    svc = service(HalcyonAssistant(client=client, model="configured-model"))
    svc._display = MagicMock()
    result = finish(svc, svc.start("teach me"))
    assert result["status"] == "failed"
    assert result["result"]["diagnostic"]["request_id"] == "req_test"
    assert result["result"]["diagnostic"]["model"] == "configured-model"
    assert "req_test" in str(svc._display.call_args_list)
    assert "private provider message" not in str(result)
    assert result["verdict"]["needs_attention"]
    svc._command.assert_not_called()


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("credit_balance_exhausted", "model_credit_balance_exhausted"),
        ("rate_limit_exceeded", "model_rate_limited"),
        ("slow_down", "model_rate_limited"),
        ("insufficient_quota", "model_rate_or_quota_limit"),
    ],
)
def test_credit_exhaustion_is_actionable_and_distinct_from_temporary_throttling(code, category):
    import httpx
    from openai import RateLimitError

    client = MagicMock()
    client.responses.create.side_effect = RateLimitError(
        "private provider message",
        response=httpx.Response(
            429,
            request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
            headers={"x-request-id": "req_billing"},
        ),
        body={"code": code, "type": "insufficient_quota"},
    )
    svc = service(HalcyonAssistant(client=client, model="gpt-5.5"))
    result = finish(
        svc, svc.start("Can you please teach me Avril 14th by Aphex Twin? I’m a beginner.")
    )
    assert result["status"] == "failed"
    assert result["result"]["stopped_reason"] == category
    assert result["result"]["diagnostic"]["api_code"] == code
    assert result["result"]["steps"] == []
    assert ("Add credits" in result["result"]["reply"]) == (code == "credit_balance_exhausted")
    assert "private provider message" not in str(result)
    client.responses.create.assert_called_once()
    svc._command.assert_not_called()


def test_closing_prevents_new_actions():
    svc = service()
    svc.close()
    assert svc.start("help")["error"]["code"] == "ASSISTANT_CLOSED"


def test_question_is_answered_while_lesson_remains_running_and_both_turns_survive():
    tracker = RunTracker()
    assistant = HalcyonAssistant(client=MagicMock())
    child_ids, question_history = [], []

    def command(_text, parent, timeout):
        child, _ = tracker.create_run(
            command="/coach teach", source="llm", timeout_s=timeout, parent_run_id=parent
        )
        tracker.mark_running(child)
        child_ids.append(child)
        return 202, tracker.get_status(child)

    def ask(goal, *, control, history, **_kwargs):
        if goal == "teach Avril":
            outcome = control("run_slash_command", {"command": '/coach teach "Avril"'})
            return AssistantResult("Demonstration ended.", [outcome])
        question_history.extend(history)
        assert tracker.get_status(child_ids[0])["status"] == "running"
        assert control("get_app_state", {})["audio"]["ready"]
        return AssistantResult("Use your right hand for this melody.")

    assistant.ask = ask
    svc = service(assistant, tracker, command)
    first = svc.start("teach Avril")
    first_worker = svc._worker
    try:
        with svc._foreground:
            assert svc._foreground.wait_for(lambda: svc._active_run_id is None, timeout=1)
        question = svc.start("Which hand?", session_id=first["session_id"])
        assert question["started"]
        assert finish(svc, question)["status"] == "completed"
        assert tracker.get_status(first["run_id"])["status"] == "running"
        assert any(item["content"] == "teach Avril" for item in question_history)
        assert any("still running" in item["content"] for item in question_history)
    finally:
        for child in child_ids:
            tracker.mark_completed(child, success=True, result={"ok": True})
        first_worker.join(timeout=2)
    assert not first_worker.is_alive()
    assert tracker.get_status(first["run_id"])["status"] == "completed"
    history = str(svc._sessions[first["session_id"]])
    assert "Which hand?" in history
    assert "Use your right hand" in history
    assert "Demonstration ended" in history


@pytest.mark.parametrize(
    "goal", ["stop", "Please stop!", "Can you stop the lesson?", "stop playing"]
)
def test_stop_bypasses_inflight_model_and_blocks_its_late_actions(goal):
    entered, release = Event(), Event()
    client = MagicMock()

    def model(**_kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "run_slash_command",
                    "call_id": "late",
                    "arguments": '{"command": "/coach replay"}',
                }
            ],
        }

    client.responses.create.side_effect = model
    tracker = RunTracker()
    commands = []

    def command(text, parent, timeout):
        commands.append(text)
        child, _ = tracker.create_run(
            command=text, source="llm", parent_run_id=parent, timeout_s=timeout
        )
        tracker.mark_completed(
            child,
            success=True,
            result={
                "ok": True,
                "code": "COACH_STOPPED",
                "message": "Coach stopped and voices released.",
            },
        )
        return 200, tracker.get_status(child)

    svc = service(HalcyonAssistant(client=client), tracker, command)
    first = svc.start("teach Avril")
    first_worker = svc._worker
    assert entered.wait(timeout=1)
    try:
        stop = svc.start(goal, session_id=first["session_id"])
        assert stop["started"]
        result = finish(svc, stop)
        assert result["status"] == "completed"
        assert "voices released" in result["result"]["reply"]
        assert tracker.get_status(first["run_id"])["status"] == "cancelled"
        client.responses.create.assert_called_once()
    finally:
        release.set()
        first_worker.join(timeout=2)
    assert not first_worker.is_alive()
    assert commands == ["/coach stop"]
    assert svc._active_run_id is None


@pytest.mark.parametrize(
    "goal", ["don't stop", "How do I stop?", "What does stop do?", "stop being so vague"]
)
def test_mentions_of_stop_are_not_treated_as_stop_requests(goal):
    assistant = HalcyonAssistant(client=MagicMock())
    assistant.ask = MagicMock(return_value=AssistantResult("An answer."))
    svc = service(assistant)
    assert finish(svc, svc.start(goal))["status"] == "completed"
    assistant.ask.assert_called_once()
    svc._command.assert_not_called()


def test_stop_failure_is_reported_without_a_model_or_claiming_audio_stopped():
    assistant = HalcyonAssistant(api_key="")
    svc = service(
        assistant,
        command=MagicMock(
            return_value=(
                400,
                {"ok": False, "error": {"code": "STOP_FAILED", "message": "Stop failed."}},
            )
        ),
    )
    result = finish(svc, svc.start("please stop"))
    assert result["status"] == "failed"
    assert result["result"]["stopped_reason"] == "stop_failed"
    assert "STOP_FAILED" in result["result"]["reply"]
    svc._command.assert_called_once()


def test_stop_interrupts_waiting_lesson_without_waiting_for_its_terminal_result():
    tracker = RunTracker()
    assistant = HalcyonAssistant(client=MagicMock())
    children, commands = [], []

    def command(text, parent, timeout):
        commands.append(text)
        child, _ = tracker.create_run(
            command=text, source="llm", timeout_s=timeout, parent_run_id=parent
        )
        tracker.mark_running(child)
        if text == "/coach stop":
            tracker.mark_cancelled(children[0])
            tracker.mark_completed(child, success=True, result={"ok": True, "message": "Stopped."})
        else:
            children.append(child)
        return 202, tracker.get_status(child)

    def ask(_goal, *, control, **_kwargs):
        outcome = control("run_slash_command", {"command": "/coach replay"})
        return AssistantResult("Playback ended.", [outcome])

    assistant.ask = MagicMock(side_effect=ask)
    svc = service(assistant, tracker, command)
    first = svc.start("replay")
    first_worker = svc._worker
    try:
        with svc._foreground:
            assert svc._foreground.wait_for(lambda: svc._active_run_id is None, timeout=1)
        stop = svc.start("stop", session_id=first["session_id"])
        assert finish(svc, stop)["status"] == "completed"
        assert tracker.get_status(first["run_id"])["status"] == "cancelled"
        assert tracker.get_status(children[0])["status"] == "cancelled"
        assistant.ask.assert_called_once()
        assert commands == ["/coach replay", "/coach stop"]
    finally:
        svc.close()
        first_worker.join(timeout=2)
    assert not first_worker.is_alive()


def test_lesson_completion_waits_for_foreground_question_before_model_continuation():
    tracker = RunTracker()
    assistant = HalcyonAssistant(client=MagicMock())
    question_entered, release_question, lesson_returned, terminal_observed = (
        Event(),
        Event(),
        Event(),
        Event(),
    )
    children = []
    original_wait = tracker.wait_for_run

    def wait(child, **kwargs):
        result = original_wait(child, **kwargs)
        if result and result.get("terminal"):
            terminal_observed.set()
        return result

    tracker.wait_for_run = wait

    def command(text, parent, timeout):
        child, _ = tracker.create_run(
            command=text, source="llm", timeout_s=timeout, parent_run_id=parent
        )
        tracker.mark_running(child)
        children.append(child)
        return 202, tracker.get_status(child)

    def ask(goal, *, control, **_kwargs):
        if goal == "replay":
            control("run_slash_command", {"command": "/coach replay"})
            lesson_returned.set()
            return AssistantResult("Playback completed.")
        question_entered.set()
        assert release_question.wait(timeout=2)
        return AssistantResult("Question answered.")

    assistant.ask = ask
    svc = service(assistant, tracker, command)
    first = svc.start("replay")
    first_worker = svc._worker
    try:
        with svc._foreground:
            assert svc._foreground.wait_for(lambda: svc._active_run_id is None, timeout=1)
        question = svc.start("How do I play this?", session_id=first["session_id"])
        assert question_entered.wait(timeout=1)
        tracker.mark_completed(children[0], success=True, result={"ok": True})
        assert terminal_observed.wait(timeout=1)
        assert not lesson_returned.is_set()
        assert svc._active_run_id == question["run_id"]
        release_question.set()
        assert finish(svc, question)["status"] == "completed"
    finally:
        release_question.set()
        if not terminal_observed.is_set():
            svc.close()
        first_worker.join(timeout=2)
    assert not first_worker.is_alive()
    assert lesson_returned.is_set()
    assert tracker.get_status(first["run_id"])["status"] == "completed"


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

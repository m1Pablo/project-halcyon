from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import threading

import pytest

from halcyon.assistant import AssistantResult
from halcyon.chat_widgets import ChatPanel, ConsoleChunk
from halcyon.command_executor import (
    CommandProgress,
    ImmediateCommandOutcome,
    TerminalCommandOutcome,
)
from halcyon.console_events import ConsoleEvent
from halcyon.openai_credentials import CredentialSource
from test_coach_integration import make_pending
from test_openai_credentials_integration import MemorySecretBackend, make_app, make_store


@pytest.fixture
def console_app(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, make_store(MemorySecretBackend()), suffix="console")
    yield app
    app.on_unmount()


def commands(app):
    return [event for event in app.chat_panel.transcript.entries if event.kind == "command"]


def test_typed_rerun_and_external_slash_use_one_executor_and_truthful_runs(console_app):
    app = console_app
    calls = []
    app._execute_tool_from_worker = lambda name, args: (
        calls.append((name, args)) or ImmediateCommandOutcome({"ok": True})
    )
    original = "/synth set   tone 0.25"
    app.on_chat_panel_submitted(ChatPanel.Submitted(original))
    first = commands(app)[0]
    assert first.status == "completed"
    assert first.text == first.replay_command == original
    assert first.source == "You" and first.local_origin == "chat"
    app.on_chat_panel_rerun_requested(ChatPanel.RerunRequested(first.replay_command))
    code, external = app.mcp_local_run_slash(command="/panic", source="codex")
    assert code == 200
    entries = commands(app)
    assert len(entries) == len(calls) == 3
    assert len({event.run_id for event in entries}) == 3
    assert entries[1].source == "You · rerun"
    assert entries[2].source == "codex" and entries[2].role == "client"
    assert entries[2].run_id == external["run_id"]
    for event in entries:
        status = app.mcp_local_run_status(event.run_id)
        logs = app.mcp_local_run_logs(event.run_id)
        waited = app.mcp_local_wait_for_run(event.run_id)
        assert status["run_id"] == logs["run_id"] == waited["run_id"] == event.run_id
        assert status["status"] == event.status
        assert status["source"] in {"llm", "codex"}
        public = json.dumps([status, logs, waited, app.mcp_local_state()])
        assert "local_origin" not in public and "replay_command" not in public
        assert original not in public
    assert len(calls) == 3  # Polling, recall, and copy have no execution authority.
    assert app._normalize_mcp_source("chat") == "llm"
    assert app._normalize_mcp_source("rerun") == "llm"


def test_invalid_group_and_revalidation_do_not_dispatch(console_app):
    app = console_app
    calls = []
    app._execute_tool_from_worker = lambda *args: calls.append(args)
    assert not app._execute_chat_slash_command("/preset")
    assert not app._execute_chat_slash_command("/not-a-command")
    app.on_chat_panel_rerun_requested(ChatPanel.RerunRequested("/not-a-command"))
    assert calls == []
    assert commands(app) == []


def test_copy_history_and_polling_never_dispatch(console_app, monkeypatch):
    async def run():
        app = console_app
        calls, copied = [], []
        monkeypatch.setattr(
            "halcyon.chat_widgets.copy_text", lambda _app, text: copied.append(text)
        )
        app._execute_tool_from_worker = lambda *args: (
            calls.append(args) or ImmediateCommandOutcome({"ok": True})
        )
        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.press("f4")
            app.chat_panel.input.value = "/panic"
            app.chat_panel.submit_input()
            await pilot.pause()
            event = commands(app)[0]
            app.chat_panel.input.value = "unsent draft"
            assert app.chat_panel.recall_history(-1)
            assert app.chat_panel.input.value == "/panic"
            assert app.chat_panel.recall_history(1)
            assert app.chat_panel.input.value == "unsent draft"
            transcript = app.chat_panel.transcript
            chunk = next(
                item for item in transcript.query(ConsoleChunk) if item.event.kind == "command"
            )
            transcript.show_context_menu(chunk, "", 0, 0)
            await pilot.pause()
            await pilot.click("#console-copy-entry")
            assert copied == ["/panic"]
            app.mcp_local_run_status(event.run_id)
            app.mcp_local_run_logs(event.run_id)
            app.mcp_local_wait_for_run(event.run_id)
            assert len(calls) == len(commands(app)) == 1

    asyncio.run(run())


def test_quoted_spacing_is_preserved_and_multiple_commands_do_not_dispatch(console_app):
    app = console_app
    calls = []
    app._execute_tool_from_worker = lambda name, args: (
        calls.append((name, args)) or ImmediateCommandOutcome({"ok": True})
    )
    assert app._execute_chat_slash_command('/coach teach "Two  Words" --segment "main  melody"')
    assert calls[0][1]["query"] == "Two  Words"
    assert calls[0][1]["segment"] == "main  melody"
    assert not app._execute_chat_slash_command("/panic\n/panic")
    assert len(calls) == len(commands(app)) == 1


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled", "timed_out"])
def test_pending_lifecycle_uses_authoritative_settlement_once(console_app, terminal):
    app = console_app
    harness = make_pending()
    calls = []
    app._execute_tool_from_worker = lambda *args: calls.append(args) or harness.pending
    command = "/coach teach Test Melody --segment main"
    assert app._execute_chat_slash_command(command)
    first = commands(app)[0]
    code, duplicate = app.mcp_local_run_slash(command=command, source="claude")
    assert code == 200 and duplicate["deduplicated"]
    assert duplicate["run_id"] == first.run_id and len(calls) == 1
    assert app._execute_chat_slash_command(command)
    assert len(commands(app)) == len(calls) == 1
    assert any(
        event.kind == "log" and event.run_id == first.run_id and "Already running" in event.text
        for event in app.chat_panel.transcript.entries
    )
    harness.subscribers[0](
        CommandProgress("operation-test", "session-test", "demonstrating", "Playing", 25)
    )
    assert commands(app)[0].status == "running"
    if terminal == "timed_out":
        app._run_tracker.mark_timeout(first.run_id)
        assert commands(app)[0].status == "running"
    harness.completion.set_result(
        TerminalCommandOutcome(
            "operation-test",
            "session-test",
            "cancelled" if terminal == "timed_out" else terminal,
            {"ok": terminal == "completed", "message": "Settled"},
        )
    )
    event = commands(app)[0]
    assert event.status == terminal
    assert event.operation_id == "operation-test" and event.session_id == "session-test"
    assert app.mcp_local_run_status(event.run_id)["status"] == terminal
    before = list(app.chat_panel.transcript.entries)
    harness.subscribers[0](
        CommandProgress("operation-test", "session-test", "late", "Late success", 100)
    )
    app._receive_console_event(replace(event, status="running", event_id="stale-running"))
    assert app.chat_panel.transcript.entries == before
    assert len(commands(app)) == 1 and harness.unsubscribe_calls == [1]


def test_private_replay_and_known_credentials_never_escape(console_app):
    app = console_app
    secret = "unusual-active-credential"
    app.assistant.set_api_key(secret)
    app._execute_tool_from_worker = lambda *_: ImmediateCommandOutcome({"ok": True})
    app._execute_chat_slash_command(f'/coach teach "{secret} /Users/private/song" --segment main')
    event = commands(app)[0]
    assert secret not in repr(event) and "/Users/private/song" not in repr(event)
    assert event.replay_command is None
    assert secret not in repr(app._console_runs)
    assert secret not in json.dumps(app.mcp_local_state())
    for index in range(1_010):
        app._remember_console_run(str(index), "/panic", "chat")
    assert len(app._console_runs) == 1_000


def test_natural_and_external_assistant_requests_share_one_visible_history(console_app):
    async def run():
        app = console_app
        asks, executions = [], []
        goal = "make it softer\n\nkeep  the pulse"
        reply = "Tone changed.\n\n```python\ntone = 0.2\n```"

        def ask(goal, *, control, **_kwargs):
            asks.append(goal)
            child = control("run_slash_command", {"command": "/synth set tone 0.2"})
            return AssistantResult(reply, tool_results=[child])

        app.assistant.ask = ask
        app._execute_tool_from_worker = lambda *args: (
            executions.append(args) or ImmediateCommandOutcome({"ok": True})
        )
        async with app.run_test(size=(120, 42)) as pilot:
            app._submit_natural_chat(goal)
            for _ in range(100):
                await asyncio.sleep(0.01)
                if any(
                    e.kind == "message" and e.role == "assistant"
                    for e in app.chat_panel.transcript.entries
                ):
                    break
            session = app._chat_assistant_session_id
            code, second = app.mcp_local_run_slash(
                command=f'/assistant ask "softer again" --session-id {session}', source="codex"
            )
            assert code == 202
            for _ in range(100):
                await asyncio.sleep(0.01)
                if (
                    len(
                        [
                            e
                            for e in app.chat_panel.transcript.entries
                            if e.kind == "message" and e.role == "assistant"
                        ]
                    )
                    == 2
                ):
                    break
            await pilot.pause()
            entries = app.chat_panel.transcript.entries
            requests = [e for e in entries if e.kind == "message" and e.role == "user"]
            replies = [e for e in entries if e.kind == "message" and e.role == "assistant"]
            assert [e.text for e in requests] == [goal, "softer again"]
            assert [e.text for e in replies] == [reply, reply]
            assert requests[0].source == "You" and requests[1].source == "codex"
            assert requests[1].run_id == replies[1].run_id == second["run_id"]
            assert len(asks) == len(executions) == 2
            assert asks == [goal, "softer again"]
            assert len(commands(app)) == 4
            for request in requests:
                children = [e for e in commands(app) if e.parent_run_id == request.run_id]
                assert len(children) == 1 and children[0].source == "Assistant"
                assert children[0].status == "completed"
                assert (
                    app.mcp_local_run_status(children[0].run_id)["parent_run_id"] == request.run_id
                )

    asyncio.run(run())


def test_dependency_failure_has_one_truthful_reply_and_no_stale_running_request(console_app):
    async def run():
        app = console_app
        calls = []

        def unavailable(goal, **_kwargs):
            calls.append(goal)
            raise ImportError("synthetic dependency sk-private /Users/private/dependency")

        app.assistant.ask = unavailable
        async with app.run_test(size=(122, 60)) as pilot:
            assert not app.query_one("#command-console").query(".panel-title")
            app._submit_natural_chat("Help me practice this transition")
            async with asyncio.timeout(2):
                while not any(
                    event.kind == "message" and event.role == "assistant"
                    for event in app.chat_panel.transcript.entries
                ):
                    await pilot.pause(0.01)
            await pilot.pause()
            entries = app.chat_panel.transcript.entries
            requests = [e for e in entries if e.kind == "message" and e.role == "user"]
            replies = [e for e in entries if e.kind == "message" and e.role == "assistant"]
            assert calls == ["Help me practice this transition"]
            assert len(requests) == len(replies) == 1
            request, reply = requests[0], replies[0]
            assert request.source == "You" and request.status is None
            assert reply.source == "Halcyon" and reply.status == "failed"
            assert request.run_id == reply.run_id
            assert app.mcp_local_run_status(reply.run_id)["status"] == "failed"
            related = [e for e in entries if e.run_id == reply.run_id]
            assert all(e.status not in {"queued", "running"} for e in related)
            assert len([e for e in related if "dependency is unavailable" in e.text]) == 1
            assert len([e for e in related if e.kind == "command" and e.status == "failed"]) == 1
            chunks = [
                chunk
                for chunk in app.chat_panel.transcript.query(ConsoleChunk)
                if chunk.event.run_id == reply.run_id
            ]
            assert all("running" not in chunk.label_text() for chunk in chunks)
            assert not any("Halcyon · You" in chunk.label_text() for chunk in chunks)
            assert "sk-private" not in repr(related)
            assert "/Users/private" not in repr(related)

    asyncio.run(run())


def test_credential_announcements_do_not_claim_provider_readiness(console_app):
    app = console_app
    for action in ("load", "save"):
        app._announce_openai_credential_result(
            action,
            was_enabled=False,
            enabled=True,
            source=CredentialSource.SYSTEM_KEYRING,
        )
    assert [event.text for event in app.chat_panel.transcript.entries] == [
        "OpenAI credential loaded securely.",
        "OpenAI credential saved securely.",
    ]


def test_busy_assistant_keeps_emergency_commands_and_ignores_late_reply(console_app):
    async def run():
        app = console_app
        started, release = threading.Event(), threading.Event()
        calls = []

        def ask(*_args, **_kwargs):
            started.set()
            release.wait(2)
            return AssistantResult("Late reply must not render")

        app.assistant.ask = ask
        app._execute_tool_from_worker = lambda name, args: (
            calls.append(name) or ImmediateCommandOutcome({"ok": True})
        )
        async with app.run_test(size=(90, 30)) as pilot:
            app._submit_natural_chat("wait")
            assert started.wait(0.5)
            assert app._execute_chat_slash_command("/panic")
            assert app._execute_chat_slash_command("/coach stop")
            assert calls == ["all_notes_off", "stop_coach_session"]
            run = next(e for e in commands(app) if e.text.startswith("/assistant ask"))
            app._run_tracker.mark_timeout(run.run_id)
            release.set()
            await asyncio.sleep(0.05)
            await pilot.pause()
            assert not any(
                e.kind == "message" and e.role == "assistant"
                for e in app.chat_panel.transcript.entries
            )

    asyncio.run(run())


def test_observer_marshalling_is_nonblocking_and_unsubscribes_on_close(console_app):
    async def run():
        app = console_app
        async with app.run_test(size=(90, 30)) as pilot:
            run, _ = app._run_tracker.create_run(command="/panic", source="codex")
            done = threading.Event()
            thread = threading.Thread(
                target=lambda: (
                    app._run_tracker.record_log_event({"run_id": run, "message": "worker log"}),
                    done.set(),
                )
            )
            thread.start()
            thread.join(0.5)
            assert done.is_set()  # A blocking call_from_thread sink would wait on this UI thread.
            await pilot.pause()
            assert any(e.text == "worker log" for e in app.chat_panel.transcript.entries)
            before = list(app.chat_panel.transcript.entries)
        assert app._resources_closed
        app._run_tracker.mark_completed(run)
        app._receive_console_event(ConsoleEvent("late", "log", "system", "system", "Late"))
        assert app.chat_panel.transcript.entries == before

    asyncio.run(run())


@pytest.mark.parametrize("size", [(180, 55), (120, 42), (90, 30), (120, 30)])
def test_console_editor_comma_menu_and_workspace_geometry(console_app, size):
    async def run():
        app = console_app
        async with app.run_test(size=size) as pilot:
            if app.screen.has_class("-compact"):
                await pilot.press("f4")
            app.chat_panel.input.focus()
            await pilot.press("h", "i", "comma", "space", "x")
            assert app.chat_panel.input.text == "hi, x"
            assert app.screen is not app.settings_screen
            assert app.query_one("#chat-input").region.height >= 4
            assert not app.query("#chat-send")
            app.chat_panel.input.value = "/c"
            app.chat_panel.input.cursor_position = 2
            await pilot.pause()
            suggestions = app.query_one("#chat-suggestions")
            assert 0 < suggestions.region.height <= (3 if app.screen.has_class("-compact") else 5)
            assert suggestions.region.bottom <= app.query_one("#chat-input-row").region.y
            assert (
                app.query_one("#chat-input").region.bottom
                <= app.query_one("#command-console").region.bottom
            )
            app._execute_chat_slash_command("/panic")
            await pilot.pause()
            transcript = app.chat_panel.transcript
            chunk = next(
                chunk for chunk in transcript.query(ConsoleChunk) if chunk.event.kind == "command"
            )
            transcript.show_context_menu(chunk, "", size[0] - 1, size[1] - 1)
            await pilot.pause()
            menu = app.screen.query_one("#console-context-menu")
            assert menu.region.right <= size[0] and menu.region.bottom <= size[1]
            await pilot.press("escape")
            if app.screen.has_class("-compact"):
                await pilot.press("f3")
                assert app.query_one("#activity-panel").display
                await pilot.press("f1")
                assert (
                    app.query_one("#performance-stage").region
                    == app.query_one("#workspace-host").region
                )

    asyncio.run(run())

from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock

from halcyon.assistant import (
    MAX_ASSISTANT_OPERATION_TEXT,
    DISABLED_MESSAGE,
    HalcyonAssistant,
    openai_tool_definitions,
    render_command_accepted,
    render_command_progress,
    render_command_terminal,
)
from halcyon.command_executor import CommandProgress, PendingCommandOutcome, TerminalCommandOutcome
from halcyon.menu import load_menu


def test_openai_tool_definitions_expose_no_raw_synth_tools() -> None:
    assert {tool["name"] for tool in openai_tool_definitions()} == {
        "get_app_state",
        "run_slash_command",
    }


def test_assistant_answers_questions_without_changing_state() -> None:
    client = MagicMock()
    client.responses.create.return_value = SimpleNamespace(
        status="completed", output=[], output_text="Try a slower tempo."
    )
    control = MagicMock()
    assistant = HalcyonAssistant(menu_def=load_menu(), api_key="sk-test", client=client)
    result = assistant.ask("how should I practice?", control=control)
    assert result.text == "Try a slower tempo."
    assert result.tool_results == []
    assert result.enabled
    control.assert_not_called()


def test_assistant_passes_live_context_to_agent() -> None:
    client = MagicMock()
    client.responses.create.return_value = SimpleNamespace(
        status="completed", output=[], output_text="The current tone is dark."
    )
    assistant = HalcyonAssistant(
        menu_def=load_menu(),
        api_key="sk-test",
        client=client,
        context_provider=lambda: {
            "patch": {"preset": None, "params": [{"id": "tone", "normalized": 0.22}]},
            "presets": {"built_in": ["Init"], "user": []},
        },
    )

    assistant.ask("how dark is this?", control=MagicMock())
    prompt = client.responses.create.call_args.kwargs["instructions"]
    assert '"normalized": 0.22' in prompt
    assert '"user": []' in prompt


def test_assistant_passes_caller_owned_session_history() -> None:
    client = MagicMock()
    client.responses.create.return_value = SimpleNamespace(
        status="completed", output=[], output_text="Darker."
    )
    assistant = HalcyonAssistant(api_key="sk-test", client=client)
    history = [
        {"role": "user", "content": "make a Deep Time patch"},
        {"role": "assistant", "content": "Set tone to .34. Completed."},
    ]
    assistant.ask("make it darker", control=MagicMock(), history=history)
    inputs = client.responses.create.call_args.kwargs["input"]
    assert inputs[:2] == history


def test_assistant_missing_key_is_deterministic(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assistant = HalcyonAssistant(menu_def=load_menu())

    result = assistant.ask("do the subtle thing and also another subtle thing", control=MagicMock())

    assert result.text == DISABLED_MESSAGE
    assert result.stopped_reason == "missing_key"
    assert not result.enabled


def test_assistant_natural_language_requires_openai_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assistant = HalcyonAssistant(menu_def=load_menu())

    result = assistant.ask("all notes off", control=MagicMock())

    assert result.text == DISABLED_MESSAGE
    assert not result.enabled


def test_assistant_reconfigures_key_live(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assistant = HalcyonAssistant(menu_def=load_menu())
    assert not assistant.enabled

    assistant.set_api_key("  stored-key-one  ")
    assert assistant.enabled
    assistant.set_api_key("stored-key-two")

    assistant.set_api_key("   ")
    assert not assistant.enabled


def test_assistant_renders_accepted_and_progress_without_raw_payloads() -> None:
    completion: Future[TerminalCommandOutcome] = Future()
    pending = PendingCommandOutcome(
        operation_id="operation-123",
        session_id="session-123",
        accepted={
            "ok": True,
            "operation": {"operation_id": "operation-123", "status": "running"},
            "coach": {"phase": "resolving"},
            "raw_attempt": [1, 2, 3],
        },
        completion=completion,
        cancel_callback=lambda _reason: True,
    )
    progress = CommandProgress(
        "operation-123",
        "session-123",
        "authoring",
        "Creating one bounded approximation",
        10,
    )

    accepted_text = render_command_accepted(pending)
    progress_text = render_command_progress(progress)

    assert accepted_text == "Coach accepted (operation-123). Phase: resolving."
    assert progress_text == "Coach authoring 10%: Creating one bounded approximation"
    assert "raw_attempt" not in accepted_text


def test_assistant_terminal_renderer_whitelists_aggregate_metrics_and_three_tips() -> None:
    terminal = TerminalCommandOutcome(
        operation_id="operation-123",
        session_id="session-123",
        status="completed",
        public={
            "ok": True,
            "report": {
                "metrics": {
                    "overall_score_percent": 91.5,
                    "pitch_accuracy_percent": 96.0,
                    "completion_percent": 88.0,
                },
                "tips": ["steady pulse", "hold longer", "repeat slowly", "must not render"],
                "observed_events": [{"note": 60, "private_path": "/secret"}],
            },
        },
    )

    rendered = render_command_terminal(terminal)

    assert "overall 91.5%" in rendered
    assert "pitch 96%" in rendered
    assert "completion 88%" in rendered
    assert "steady pulse; hold longer; repeat slowly" in rendered
    assert "must not render" not in rendered
    assert "observed_events" not in rendered
    assert "/secret" not in rendered
    assert len(rendered) <= MAX_ASSISTANT_OPERATION_TEXT


def test_assistant_terminal_renderer_bounds_failures_and_marks_timeout() -> None:
    terminal = TerminalCommandOutcome(
        operation_id="operation-123",
        session_id="session-123",
        status="timed_out",
        public={
            "ok": False,
            "error": {
                "code": "SESSION_CANCELLED",
                "message": "cleanup settled " + "x" * 2_000,
                "private": "/secret/path",
            },
        },
    )

    rendered = render_command_terminal(terminal)

    assert rendered.startswith("Coach timed out [SESSION_CANCELLED]: cleanup settled")
    assert "/secret/path" not in rendered
    assert len(rendered) <= MAX_ASSISTANT_OPERATION_TEXT

    failed = TerminalCommandOutcome(
        operation_id="operation-456",
        session_id="session-456",
        status="failed",
        public={
            "ok": False,
            "error": {"code": "AUDIO_BACKPRESSURE", "message": "Cleanup admission failed."},
        },
    )
    cancelled = TerminalCommandOutcome(
        operation_id="operation-789",
        session_id="session-789",
        status="cancelled",
        public={
            "ok": False,
            "error": {"code": "SESSION_CANCELLED", "message": "Cleanup completed."},
        },
    )

    assert render_command_terminal(failed).startswith("Coach failed [AUDIO_BACKPRESSURE]")
    assert render_command_terminal(cancelled).startswith("Coach stopped [SESSION_CANCELLED]")


def test_assistant_terminal_renderer_surfaces_only_bounded_source_resolution() -> None:
    terminal = TerminalCommandOutcome(
        operation_id="operation-source",
        session_id="session-source",
        status="failed",
        public={
            "ok": False,
            "error": {
                "code": "SOURCE_REQUIRED",
                "message": "No saved lesson matches this cache-only request.",
                "resolution": (
                    'Retry without --author never: /coach teach "work title" '
                    '--segment "main melody".'
                ),
                "private_path": "/private/lesson.json",
                "raw_prompt": "PRIVATE_AUTHORING_PROMPT",
            },
        },
    )

    rendered = render_command_terminal(terminal)

    assert rendered == (
        "Coach needs input [SOURCE_REQUIRED]: No saved lesson matches this cache-only "
        'request. Next: Retry without --author never: /coach teach "work title" '
        '--segment "main melody".'
    )
    assert "/private/lesson.json" not in rendered
    assert "PRIVATE_AUTHORING_PROMPT" not in rendered
    assert len(rendered) <= MAX_ASSISTANT_OPERATION_TEXT


def test_assistant_bounds_reply_and_scrubs_nonstandard_key() -> None:
    client = MagicMock()
    client.responses.create.return_value = SimpleNamespace(
        status="completed", output=[], output_text="private-key-value " + "x" * 10_000
    )
    assistant = HalcyonAssistant(api_key="private-key-value", client=client)
    result = assistant.ask("hello", control=MagicMock())
    assert len(result.text) == 2_000
    assert "private-key-value" not in result.text


def test_assistant_ignores_non_dict_context_provider_result() -> None:
    client = MagicMock()
    client.responses.create.return_value = SimpleNamespace(
        status="completed", output=[], output_text="Ready."
    )
    assistant = HalcyonAssistant(
        menu_def=load_menu(),
        api_key="sk-test",
        client=client,
        context_provider=lambda: ["PRIVATE_RAW_CONTEXT"],  # type: ignore[arg-type,return-value]
    )

    assistant.ask("what is ready?", control=MagicMock())
    assert "PRIVATE_RAW_CONTEXT" not in str(client.responses.create.call_args.kwargs)

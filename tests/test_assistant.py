import json
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import halcyon.assistant as assistant_module
from halcyon.assistant import (
    MAX_ASSISTANT_HISTORY_ENTRIES,
    MAX_ASSISTANT_HISTORY_TEXT,
    MAX_ASSISTANT_OPERATION_TEXT,
    DISABLED_MESSAGE,
    HalcyonAssistant,
    openai_tool_definitions,
    render_command_accepted,
    render_command_progress,
    render_command_terminal,
)
from halcyon.chat_router import MISSING_KEY_REASON, RoutingDecision
from halcyon.command_executor import CommandProgress, PendingCommandOutcome, TerminalCommandOutcome
from halcyon.menu import load_menu

_LLM = "halcyon.chat_router._call_router_llm"


def test_openai_tool_definitions_expose_no_raw_synth_tools() -> None:
    assert openai_tool_definitions() == []


def test_assistant_returns_routing_decision_from_mocked_openai() -> None:
    payload = {
        "action": "menu",
        "stages": [["/synth patch tone=0.85 release=0.05"]],
        "reason": "brighter and shorter",
    }
    llm = MagicMock(return_value=json.dumps(payload))
    assistant = HalcyonAssistant(menu_def=load_menu(), api_key="sk-test", client=object())

    with patch(_LLM, llm):
        result = assistant.ask("make it brighter and shorter")

    assert result.text == "Routing to `/synth patch tone=0.85 release=0.05`."
    assert result.decision.commands == ("/synth patch tone=0.85 release=0.05",)
    assert result.tool_results == []
    assert result.enabled


def test_assistant_passes_live_context_to_router_prompt() -> None:
    payload = {
        "action": "menu",
        "stages": [["/preset save Deep Time"]],
        "reason": "save current patch",
    }
    llm = MagicMock(return_value=json.dumps(payload))
    assistant = HalcyonAssistant(
        menu_def=load_menu(),
        api_key="sk-test",
        client=object(),
        context_provider=lambda: {
            "patch": {"preset": None, "params": [{"id": "tone", "normalized": 0.22}]},
            "presets": {"built_in": ["Init"], "user": []},
        },
    )

    with patch(_LLM, llm):
        result = assistant.ask("save this preset as Deep Time")

    prompt = llm.call_args.args[0]
    assert '"normalized":0.22' in prompt
    assert '"user":[]' in prompt
    assert result.decision.commands == ("/preset save Deep Time",)


def test_assistant_passes_recent_chat_context_to_router_prompt() -> None:
    llm = MagicMock(
        side_effect=[
            json.dumps(
                {
                    "action": "menu",
                    "stages": [["/synth patch tone=0.34 release=0.58"]],
                    "reason": "initial patch",
                }
            ),
            json.dumps(
                {
                    "action": "menu",
                    "stages": [["/synth set tone 0.22"]],
                    "reason": "relative adjustment",
                }
            ),
        ]
    )
    assistant = HalcyonAssistant(menu_def=load_menu(), api_key="sk-test", client=object())

    with patch(_LLM, llm):
        assistant.ask("make a Deep Time patch")
        result = assistant.ask("make it darker")

    second_prompt = llm.call_args_list[1].args[0]
    assert "make a Deep Time patch" in second_prompt
    assert "/synth patch tone=0.34 release=0.58" in second_prompt
    assert result.decision.commands == ("/synth set tone 0.22",)


def test_assistant_missing_key_is_deterministic(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assistant = HalcyonAssistant(menu_def=load_menu())

    result = assistant.ask("do the subtle thing and also another subtle thing")

    assert result.text == MISSING_KEY_REASON
    assert result.text == DISABLED_MESSAGE
    assert result.decision.action == "reject"
    assert not result.enabled


def test_assistant_natural_language_requires_openai_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assistant = HalcyonAssistant(menu_def=load_menu())

    result = assistant.ask("all notes off")

    assert result.text == MISSING_KEY_REASON
    assert result.decision.action == "reject"
    assert not result.enabled


def test_assistant_reconfigures_key_live_without_losing_history(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls: list[tuple[str | None, list[dict[str, str]]]] = []

    def route(_message: str, **kwargs: object) -> RoutingDecision:
        calls.append(
            (
                kwargs.get("api_key") if isinstance(kwargs.get("api_key"), str) else None,
                list(kwargs.get("chat_context", [])),  # type: ignore[arg-type]
            )
        )
        return RoutingDecision(action="reject", reason="test")

    monkeypatch.setattr(assistant_module, "run_chat_routing", route)
    assistant = HalcyonAssistant(menu_def=load_menu())
    assert not assistant.enabled

    assistant.set_api_key("  stored-key-one  ")
    assert assistant.enabled
    assistant.ask("first request")
    assistant.set_api_key("stored-key-two")
    assistant.ask("second request")

    assert calls[0] == ("stored-key-one", [])
    assert calls[1][0] == "stored-key-two"
    assert any(item["content"] == "first request" for item in calls[1][1])

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


def test_assistant_bounds_retained_history_and_decision_text() -> None:
    llm = MagicMock(
        return_value=json.dumps({"action": "reject", "stages": [], "reason": "outside app"})
    )
    assistant = HalcyonAssistant(menu_def=load_menu(), api_key="sk-test", client=object())

    with patch(_LLM, llm):
        for index in range(6):
            assistant.ask(f"message-{index}-" + "x" * (MAX_ASSISTANT_HISTORY_TEXT + 200))

    assert len(assistant._history) == MAX_ASSISTANT_HISTORY_ENTRIES
    assert all(len(entry["content"]) <= MAX_ASSISTANT_HISTORY_TEXT for entry in assistant._history)
    assert not any("message-0" in entry["content"] for entry in assistant._history)
    assert not any("message-1" in entry["content"] for entry in assistant._history)

    rejected = assistant_module._decision_text(
        RoutingDecision(action="reject", reason="r" * 10_000)
    )
    routed = assistant_module._decision_text(
        RoutingDecision(action="menu", stages=(("/panic " + "x" * 10_000,),))
    )
    assert len(rejected) == MAX_ASSISTANT_OPERATION_TEXT
    assert len(routed) == MAX_ASSISTANT_OPERATION_TEXT


def test_assistant_ignores_non_dict_context_provider_result() -> None:
    payload = {"action": "menu", "stages": [["/panic"]], "reason": "panic"}
    llm = MagicMock(return_value=json.dumps(payload))
    assistant = HalcyonAssistant(
        menu_def=load_menu(),
        api_key="sk-test",
        client=object(),
        context_provider=lambda: ["PRIVATE_RAW_CONTEXT"],  # type: ignore[arg-type,return-value]
    )

    with patch(_LLM, llm):
        result = assistant.ask("panic")

    assert "PRIVATE_RAW_CONTEXT" not in llm.call_args.args[0]
    assert result.decision.commands == ("/panic",)

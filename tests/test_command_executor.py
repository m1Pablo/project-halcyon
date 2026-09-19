import pytest
from concurrent.futures import Future

from halcyon.command_executor import (
    CommandProgress,
    CommandExecutionError,
    ImmediateCommandOutcome,
    PendingCommandOutcome,
    TerminalCommandOutcome,
    execute_slash_command,
    execute_slash_command_outcome,
    prepare_slash_command,
)
from halcyon.menu import load_menu


class FakeHost:
    pass


def test_prepare_slash_command_maps_set_to_tool_arguments() -> None:
    invocation = prepare_slash_command(load_menu(), "/synth set tone 0.8")

    assert invocation.leaf_command == "/synth set"
    assert invocation.tool == "set_synth_param"
    assert invocation.arguments == {"param": "tone", "value": 0.8}


def test_execute_slash_command_calls_safe_tool_executor() -> None:
    calls: list[tuple[object, str, dict[str, object]]] = []

    def executor(host, tool, arguments):  # type: ignore[no-untyped-def]
        calls.append((host, tool, arguments))
        return {"ok": True, "tool": tool, "arguments": arguments}

    host = FakeHost()
    result = execute_slash_command(
        host,
        load_menu(),
        "/synth patch tone=0.7 release=0.1",
        tool_executor=executor,
    )

    assert result == {
        "ok": True,
        "tool": "apply_synth_patch",
        "arguments": {"values": {"tone": 0.7, "release": 0.1}},
    }
    assert calls == [
        (
            host,
            "apply_synth_patch",
            {"values": {"tone": 0.7, "release": 0.1}},
        )
    ]


def test_preset_names_can_be_quoted_or_unquoted() -> None:
    quoted = prepare_slash_command(load_menu(), '/preset apply "Soft Pad"')
    unquoted = prepare_slash_command(load_menu(), "/preset save Dusty Pad")

    assert quoted.arguments == {"preset": "Soft Pad"}
    assert unquoted.arguments == {"preset": "Dusty Pad"}


def test_panic_maps_to_all_notes_off() -> None:
    invocation = prepare_slash_command(load_menu(), "/panic")

    assert invocation.tool == "all_notes_off"
    assert invocation.arguments == {}


def test_group_command_does_not_execute() -> None:
    with pytest.raises(CommandExecutionError, match="Command group selected"):
        prepare_slash_command(load_menu(), "/synth")


def test_unknown_param_rejects_before_tool_call() -> None:
    calls: list[str] = []

    def executor(host, tool, arguments):  # type: ignore[no-untyped-def]
        calls.append(tool)
        return {}

    with pytest.raises(CommandExecutionError, match="unknown synth parameter"):
        execute_slash_command(
            FakeHost(),
            load_menu(),
            "/synth set brightness 0.5",
            tool_executor=executor,
        )

    assert calls == []


@pytest.mark.parametrize("value", ["-0.1", "1.1", "nan", "bad"])
def test_normalized_values_must_be_finite_zero_to_one(value: str) -> None:
    with pytest.raises(CommandExecutionError, match="normalized value"):
        prepare_slash_command(load_menu(), f"/synth set tone {value}")


def test_patch_validation_is_atomic() -> None:
    calls: list[str] = []

    def executor(host, tool, arguments):  # type: ignore[no-untyped-def]
        calls.append(tool)
        return {}

    with pytest.raises(CommandExecutionError, match="unknown synth parameter"):
        execute_slash_command(
            FakeHost(),
            load_menu(),
            "/synth patch tone=0.8 brightness=0.5",
            tool_executor=executor,
        )

    assert calls == []


def test_save_builtin_preset_is_rejected_before_tool_call() -> None:
    with pytest.raises(CommandExecutionError, match="built-in preset"):
        prepare_slash_command(load_menu(), "/preset save Lead")


def test_prepare_coach_teach_preserves_query_and_parses_all_options() -> None:
    invocation = prepare_slash_command(
        load_menu(),
        '/coach teach "Aphex Twin Avril 14th" --segment "Main Melody" '
        "--tempo 75 --difficulty foundation --hand right --loops 3 --author bounded",
    )

    assert invocation.tool == "start_coach_lesson"
    assert invocation.arguments == {
        "query": "Aphex Twin Avril 14th",
        "segment": "Main Melody",
        "tempo_percent": 75,
        "difficulty": "foundation",
        "hand": "right",
        "loops": 3,
        "author": "bounded",
    }


def test_prepare_coach_commands_use_bounded_defaults_and_optional_values() -> None:
    teach = prepare_slash_command(load_menu(), "/coach teach My Melody")
    replay = prepare_slash_command(load_menu(), "/coach replay")
    attempt = prepare_slash_command(load_menu(), "/coach attempt --count-in 0")
    feedback = prepare_slash_command(load_menu(), "/coach feedback attempt-7")

    assert teach.arguments == {
        "query": "My Melody",
        "segment": None,
        "tempo_percent": 100,
        "difficulty": None,
        "hand": None,
        "loops": 1,
        "author": "bounded",
    }
    assert replay.arguments == {"segment": None, "tempo_percent": None, "loops": None}
    assert attempt.arguments == {"segment": None, "tempo_percent": None, "count_in": 0}
    assert feedback.arguments == {"attempt_id": "attempt-7"}


def test_prepare_coach_teach_preserves_explicit_cache_only_opt_out() -> None:
    invocation = prepare_slash_command(
        load_menu(),
        '/coach teach "Saved Melody" --segment "main" --author never',
    )

    assert invocation.arguments["author"] == "never"


@pytest.mark.parametrize("attempt_id", ('""', "x" * 129))
def test_prepare_coach_feedback_rejects_invalid_attempt_id(attempt_id: str) -> None:
    with pytest.raises(CommandExecutionError) as caught:
        prepare_slash_command(load_menu(), f"/coach feedback {attempt_id}")

    assert caught.value.field == "attempt_id"
    assert "1 through 128" in str(caught.value)


@pytest.mark.parametrize(
    ("command", "field"),
    [
        ("/coach teach", "query"),
        (f"/coach teach {'x' * 161}", "query"),
        ("/coach teach tune --tempo 24", "tempo"),
        ("/coach teach tune --tempo 201", "tempo"),
        ("/coach teach tune --loops 0", "loops"),
        ("/coach teach tune --loops 9", "loops"),
        ("/coach teach tune --difficulty expert", "difficulty"),
        ("/coach teach tune --hand middle", "hand"),
        ("/coach teach tune --author always", "author"),
        ("/coach attempt --count-in 9", "count-in"),
        ("/coach replay stray", "arguments"),
        ("/coach replay --tempo", "tempo"),
        ("/coach replay --unknown value", "option"),
        ("/coach replay --loops 2 --loops 3", "loops"),
    ],
)
def test_prepare_coach_rejects_invalid_fields_with_retry_usage(command: str, field: str) -> None:
    with pytest.raises(CommandExecutionError) as caught:
        prepare_slash_command(load_menu(), command)

    assert field in (caught.value.field or str(caught.value))
    assert caught.value.resolution


@pytest.mark.parametrize(
    "command",
    [
        "/coach replay SECRET_VALUE",
        "/coach replay --tempo 100 SECRET_VALUE",
        "/coach replay --SECRET_VALUE value",
        "/coach replay --SECRET_VALUE",
        "/coach replay --SECRET_VALUE=value",
        "/coach replay --unknown SECRET_VALUE",
    ],
)
def test_coach_parser_errors_never_echo_untrusted_tokens(command: str) -> None:
    with pytest.raises(CommandExecutionError) as caught:
        prepare_slash_command(load_menu(), command)

    public = caught.value.to_public_dict()
    assert "SECRET_VALUE" not in str(caught.value)
    assert "SECRET_VALUE" not in repr(public)
    assert public["field"] in {"arguments", "option"}
    assert public["resolution"].startswith("Retry using: /coach replay")


def test_coach_parser_known_missing_option_value_keeps_actionable_guidance() -> None:
    with pytest.raises(CommandExecutionError) as caught:
        prepare_slash_command(load_menu(), "/coach replay --tempo")

    public = caught.value.to_public_dict()
    assert public["field"] == "tempo"
    assert "--tempo requires a value" in public["message"]
    assert public["resolution"].startswith("Retry using: /coach replay")


def test_typed_command_outcome_preserves_pending_without_blocking() -> None:
    completion: Future[TerminalCommandOutcome] = Future()
    progress: list[CommandProgress] = []
    subscribers = []
    pending = PendingCommandOutcome(
        operation_id="op-1",
        session_id="session-1",
        accepted={"ok": True, "code": "COACH_ACCEPTED"},
        completion=completion,
        cancel_callback=lambda reason: reason == "timeout",
        subscribe_callback=lambda callback: subscribers.append(callback) or (lambda: None),
    )

    outcome = execute_slash_command_outcome(
        FakeHost(),
        load_menu(),
        "/coach replay",
        tool_executor=lambda _host, _tool, _arguments: pending,
    )

    assert outcome is pending
    assert pending.cancel("timeout")
    pending.subscribe(progress.append)
    subscribers[0](CommandProgress("op-1", "session-1", "ready", "Ready", 25))
    assert progress[0].percent == 25
    with pytest.raises(CommandExecutionError, match="still running"):
        execute_slash_command(
            FakeHost(),
            load_menu(),
            "/coach replay",
            tool_executor=lambda _host, _tool, _arguments: pending,
        )


def test_typed_command_outcome_wraps_legacy_dict_and_terminal_public_result() -> None:
    immediate = execute_slash_command_outcome(
        FakeHost(),
        load_menu(),
        "/panic",
        tool_executor=lambda _host, _tool, _arguments: {"ok": True},
    )
    terminal = TerminalCommandOutcome(
        "op-1",
        "session-1",
        "completed",
        {"ok": True, "code": "COACH_COMPLETE"},
    )

    assert immediate == ImmediateCommandOutcome({"ok": True})
    assert terminal.to_public_dict()["code"] == "COACH_COMPLETE"

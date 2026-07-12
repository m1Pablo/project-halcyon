import pytest

from halcyon.command_executor import (
    CommandExecutionError,
    execute_slash_command,
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
